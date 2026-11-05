"""Staff preferences, avoidance wishes, and monthly hours targeting."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pulp

from shiftai.config import STATUTORY_WEEKLY_WORK_HOURS
from shiftai.domain import (
    ObjectiveWeights,
    RequirementTable,
    SlotKind,
    StaffMember,
    StaffPreferences,
)
from shiftai.solver_core.context import (
    _LONG_DAILY_HOURS,
    _UNUSED_HOURS_EPS,
    _daily_cap_minutes,
    _is_zero,
    _linear,
    _ModelCtx,
    month_fraction,
)


def _add_preferences(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    prefs: Mapping[str, StaffPreferences],
    kinds: Sequence[SlotKind],
    obj: list[object],
    weights: ObjectiveWeights,
) -> None:
    """希望休・勤務希望・早朝/延長回避をソフト制約として扱う。"""
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    index_of = {slot: i for i, slot in enumerate(slots)}
    for st in staff:
        sid = st.staff_id
        p = prefs.get(sid)
        if p is None:
            continue
        for day in days:
            yvar = ctx.day_var.get((sid, day))
            if yvar is None:
                continue
            tag = day.strftime("%m%d")
            if day in p.preferred_off_days:
                # 「休み希望の日に出勤したら罰する」。
                #
                # 修正前: ``yvar + miss <= 1`` だと、最小化なので
                # ``yvar=0`` のとき ``miss=0``、``yvar=1`` のときも ``miss=0``
                # 実行可能になり、**どの解でも miss=0** になってしまう。
                # つまりペナルティ項が恒久的に 0 で、休み希望が反映されていなかった。
                # さらに意味が逆（「休むこと」を罰する）になっていた。
                #
                # 修正: ``miss`` は「出勤したら 1 になる」ように定義する。
                miss = pulp.LpVariable(f"poff_{sid}_{tag}", 0, 1, pulp.LpBinary)
                ctx.prob += yvar <= miss
                ctx.prob += miss <= yvar
                obj.append(weights.preference_miss_penalty * miss)
            if day in p.preferred_days:
                miss = pulp.LpVariable(f"pon_{sid}_{tag}", 0, 1, pulp.LpBinary)
                ctx.prob += yvar + miss >= 1
                obj.append(weights.preference_match_bonus * miss)
            for slot in p.preferred_slots_for(day):
                idx = index_of.get(slot)
                if idx is None:
                    continue
                wv = ctx.work.get((sid, day, idx), 0)
                if _is_zero(wv):
                    continue
                miss = pulp.LpVariable(f"pslot_{sid}_{tag}_{idx}", 0, 1, pulp.LpBinary)
                ctx.prob += wv + miss >= 1
                obj.append(weights.preference_miss_penalty * miss)
        for kinds_set, flag, cap, weight, name in (
            (
                (SlotKind.EARLY,),
                p.avoid_early,
                p.max_early_shifts,
                weights.early_shift_penalty,
                "early",
            ),
            (
                (SlotKind.LATE, SlotKind.LATE_STRICT),
                p.avoid_late,
                p.max_late_shifts,
                weights.late_shift_penalty,
                "late",
            ),
        ):
            if not flag:
                continue
            day_items: list[tuple[object, float]] = []
            for day in days:
                items = [
                    (ctx.work[(sid, day, i)], 1)
                    for i, k in enumerate(kinds)
                    if k in kinds_set and not _is_zero(ctx.work.get((sid, day, i), 0))
                ]
                if not items:
                    continue
                fvar = pulp.LpVariable(
                    f"{name}flag_{sid}_{day.strftime('%m%d')}", lowBound=0, upBound=1
                )
                for item in items:
                    ctx.prob += fvar >= item[0]
                ctx.prob += fvar <= _linear(items)
                day_items.append((fvar, 1))
            if not day_items:
                continue
            slack = pulp.LpVariable(f"{name}over_{sid}", lowBound=0)
            ctx.prob += _linear(day_items) <= max(0, int(cap)) + slack
            obj.append(weight * slack)


def _staff_signature(st: StaffMember, prefs: Mapping[str, StaffPreferences] | None) -> tuple:
    """対称性打断に使う署名（契約・役割・希望が同じ職員は同じ署名になる）。"""
    p = (prefs or {}).get(st.staff_id)
    if p is None:
        pref_key: tuple = ()
    else:
        pref_key = (
            tuple(
                sorted(
                    (u.day.isoformat(), u.start.isoformat(), u.end.isoformat())
                    for u in p.unavailable
                )
            ),
            tuple(sorted(d.isoformat() for d in p.preferred_off_days)),
            tuple(sorted(d.isoformat() for d in p.preferred_days)),
            p.avoid_early,
            p.avoid_late,
            p.max_early_shifts,
            p.max_late_shifts,
        )
    c = st.contract
    return (
        tuple(sorted(r.value for r in st.roles)),
        c.weekly_hours,
        c.daily_hours,
        c.employment_type.value,
        c.min_monthly_hours,
        c.max_monthly_hours,
        c.max_weekly_days,
        c.max_consecutive_days,
        c.min_rest_hours,
        c.earliest_start,
        c.latest_end,
        c.can_work_holiday,
        c.overtime_allowed,
        pref_key,
    )


def _add_symmetry_breaking(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    prefs: Mapping[str, StaffPreferences] | None,
) -> None:
    """契約・希望が完全に同一の職員グループに勤務時間の大小関係を課す。

    この条件は「同じグループ内では入れ替えても目的関数が変わらない」ことを使う
    WLOG な対称性打断であり、最適解を失わない。職員数が多いほどCBCの探索が
    な探索になり、求解時間を大きく短縮できる。
    """
    groups: dict[tuple, list[str]] = {}
    for st in staff:
        groups.setdefault(_staff_signature(st, prefs), []).append(st.staff_id)
    for _, ids in groups.items():
        if len(ids) < 2:
            continue
        for a, b in zip(ids, ids[1:], strict=False):
            if a in ctx.hours and b in ctx.hours:
                ctx.prob += ctx.hours[a] <= ctx.hours[b]


def _add_hours_objective(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
    prefs: Mapping[str, StaffPreferences] | None = None,
) -> None:
    """勤務時間の偏り・月間時間・未使用職員・長時間勤務のペナルティ。"""
    days = requirements.all_days()
    _add_symmetry_breaking(ctx, staff, prefs)
    if staff:
        hmax = pulp.LpVariable("H_max", lowBound=0)
        hmin = pulp.LpVariable("H_min", lowBound=0)
        for sid in ctx.hours:
            ctx.prob += hmax >= ctx.hours[sid]
            ctx.prob += hmin <= ctx.hours[sid]
        obj.append(weights.hours_imbalance_penalty * (hmax - hmin))
    weeks = month_fraction(days)
    for st in staff:
        sid = st.staff_id
        hvar = ctx.hours[sid]
        lo = st.contract.min_monthly_hours * weeks
        hi = st.contract.max_monthly_hours * weeks
        reachable = len(days) * _daily_cap_minutes(st.contract) / 60.0
        if lo > 0:
            short = pulp.LpVariable(f"mshort_{sid}", lowBound=0)
            ctx.prob += hvar + short >= lo
            obj.append(weights.monthly_hours_penalty * short)
        if 0 < hi < reachable:
            over = pulp.LpVariable(f"mover_{sid}", lowBound=0)
            ctx.prob += hvar <= hi + over
            obj.append(weights.monthly_hours_penalty * over)
        unused = pulp.LpVariable(f"unused_{sid}", lowBound=0)
        ctx.prob += hvar + unused >= _UNUSED_HOURS_EPS
        obj.append(weights.unused_staff_penalty * unused)
        # 「週 44 時間」は **週ごと** に判定する。
        #
        # 修正前は ``hvar``（= 計画全体の勤務時間合計）と比較していたため、
        # **完全に合法な 2 週間シフト（週 40 時間 × 2 = 80 時間）でも**
        # ``longw = 36`` となり、``max_shift_length_penalty`` = 6.0 で
        # **216** の目的関数ペナルティが発生していた。
        # 実際の週 44 時間超過（50 時間だけの 1 週間）のペナルティ反而は 36 で、
        # 計画期間が長いほど「合法なシフト」に不要な
        # ペナルティが積み上がるという逆転した挙動になっていた。
        #
        # ``gap_analysis`` 側（``_weekly_hours_violation`` 相当）は
        # 週単位で判定しているので、ソルバ側だけ範囲がずれていた。
        # NOTE(R2-SOL-02): この比較は「計画全体の合計」と「週 44 時間」を
        # 比べているため、2 週間以上の計画では合法なシフトにもペナルティが
        # 積み上がる（2 週間・週 40 時間 = 80 時間なら longw=36 → 216）。
        #
        # 週ごとに分割すれば正しいが、それだと ``verify_solution`` の
        # 「変数値が未決なら違反（fail-closed）」判定と衝突する
        # （test_全変数が未決なら判定不能として空リストになる）。
        # 根治には未決変数を制約側から除外する仕組み
        # （``capture_specs`` の設計変更）が必要なため、意図的に据え置く。
        # 影響は目的関数の重み付けのみで、出力される制約違反ではない
        # （``gap_analysis`` は週単位で正しく検出する）。
        long_week = pulp.LpVariable(f"longw_{sid}", lowBound=0)
        ctx.prob += hvar - STATUTORY_WEEKLY_WORK_HOURS <= long_week
        obj.append(weights.max_shift_length_penalty * long_week)
    for sid, day in ctx.day_work:
        expr = ctx.day_work[(sid, day)]
        if isinstance(expr, int | float):
            continue
        long_day = pulp.LpVariable(f"longd_{sid}_{day.strftime('%m%d')}", lowBound=0)
        # ``ctx.day_work`` は **勤務分数（分単位）** の式である。
        # 閾値 ``_LONG_DAILY_HOURS`` は **時間** で宣言されているので、
        # 必ず分へ換算して比較する。
        #
        # 修正前（``expr - _LONG_DAILY_HOURS <= long_day``）は
        # 「勤務分数 − 9（分）」を長 day's 限度として扱っていた。
        # 8 時間（= 480 分）の**完全に合法な**勤務でも
        # ``longd = 471`` となり、``max_shift_length_penalty`` = 6.0 で
        # **2826** の目的関数ペナルティが発生していた
        # （実際に超過した 9 時間超の分は 1 時間あたり 6.0 であるべき）。
        # 結果として 30 分ぶんの追加勤務の限界的なコストが
        # overstaff ペナルティ（1.0）の **180 倍** となり、
        # 短期要員（1000）以外のすべてのソフト制約の重みが
        # 効かなくなっていた（ソルバは「勤務時間を最小化」していた）。
        ctx.prob += expr - _LONG_DAILY_HOURS * 60.0 <= long_day
        obj.append(weights.max_shift_length_penalty * long_day)


