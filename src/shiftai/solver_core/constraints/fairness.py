"""Workload fairness, weekend equality, and symmetry breaking constraints."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pulp

from shiftai import config
from shiftai.domain import (
    ObjectiveWeights,
    RequirementTable,
    SlotKind,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    weekly_periods,
)
from shiftai.shift_patterns import ShiftPattern
from shiftai.solver_core.context import (
    _is_zero,
    _linear,
    _ModelCtx,
)


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



def _fairness_day_flag(
    ctx: _ModelCtx,
    st: StaffMember,
    day: date,
    day_var: object,
    indices: Sequence[int],
    key: str,
) -> object:
    """「その日に区分の時間帯で 1 つ以上勤務するか」を表す 0/1 変数を返す。

    勤務ブロックそのものではなく **時間帯の集合** で判定する。
    早番 = ``SlotKind.EARLY`` の時間帯のいずれか、遅番 = ``SlotKind.LATE`` /
    ``LATE_STRICT`` の時間帯のいずれか、土曜 = ``weekday() == 5`` の日。

    ``day_var`` が定数 0（休園日など）なら 0 をそのまま返し、変数を作らない。
    """
    if _is_zero(day_var):
        return 0
    items: list[tuple[object, float]] = []
    for i in indices:
        cell = ctx.work.get((st.staff_id, day, i), 0)
        if not _is_zero(cell):
            items.append((cell, 1))
    if not items:
        return 0
    # 連続変数で足りる：0/1 だと変数の数が職員×日×区分だけ増え、
    # 28 名×7 日規模で CBC が間に合わなくなる（実際に貪欲法へ退避した）。
    tag = f"{key}_{st.staff_id}_{day.strftime('%m%d')}"
    flag = pulp.LpVariable(f"fw_{tag}", 0, 1)
    total = _linear(items)
    # PuLP の LpAffineExpression は int による除算を持たないため、
    # 「n で割る」を「1/n 倍する」で書く（時間帯 1 個の問題が落ちるのを防ぐ）。
    # これにより「1 つでも勤務したら 1」「1 つもなければ 0」がちょうど表現される。
    #
    # 制約名は必ず付ける：PuLP の自動採番（``C0012345``）は値が同じ行を
    # 別々に書き出し、CBC が "Duplicate row" でモデル全体を無効化する。
    # 実際に 28 名×7 日規模でそうなった（公平性クラス導入時の実害）。
    ctx.prob += flag >= total * (1.0 / len(items)), f"fwlo_{tag}"
    ctx.prob += flag <= total, f"fwup_{tag}"
    return flag



def _add_fairness(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
    standard: StaffingStandard | None,
    patterns: Sequence[ShiftPattern],
) -> None:
    """早番・遅番・土曜出勤の「職員間レンジ」を縮める公平性ペナルティ。

    職員 ``s``・週 ``w``・区分 ``k`` の回数 ``count_{s,w,k}`` に対し、

        max_{w,k} >= count_{s,w,k}
        min_{w,k} <= count_{s,w,k}
        objective += weight_k * Σ_w (max_{w,k} - min_{w,k})

    として **最大と最小の差（レンジ）** を最小化する。総和を均すのではなく
    レンジを縮めるのは、職員数が減っても同じ「不公平さ」を表現できるため。

    ハード制約にはしない。早番が出せる職員が 2 名しかいない園では均衡が
    物理的にありえず、ハードにすると解が消える（``hours_imbalance_penalty``
    と同じ方針）。重みを 0 にすれば変数は作られない（事実上無効）。

    週ごとに分割するのは、全期間を 1 つの袋で均すと「前半 2 週だけ偏り、
    後半で相殺される」状態を隠してしまうため。

    職員が 1 名、または対象区分の時間帯が存在しない場合でも、
    最大値・最小値の補助変数そのものを作る（値が 0 になるだけで解は変わらない）。
    これにより「重みを上げると目的関数が必ず反応する」という性質を保ち、
    重みが黙って無視される退行を防ぐ。
    """
    early_weight = weights.fairness_early_penalty
    late_weight = weights.fairness_late_penalty
    saturday_weight = weights.fairness_saturday_penalty
    if max(early_weight, late_weight, saturday_weight) <= 0:
        return
    slots = tuple(requirements.slots)
    days = requirements.all_days()
    if not slots or not days or not staff:
        return
    kinds = [standard.slot_kind(slot) for slot in slots] if standard else []
    early_indices = [i for i, k in enumerate(kinds) if k is SlotKind.EARLY]
    late_indices = [i for i, k in enumerate(kinds) if k in (SlotKind.LATE, SlotKind.LATE_STRICT)]
    periods = weekly_periods(days)
    all_indices = list(range(len(slots)))
    for key, weight, indices in (
        ("early", early_weight, early_indices),
        ("late", late_weight, late_indices),
    ):
        if weight <= 0 or not indices:
            continue
        _fairness_spread(ctx, staff, periods, obj, weight, key, indices, None)
    if saturday_weight > 0:
        saturday_days = {day for day in days if day.weekday() == config.WEEKEND_START_WEEKDAY}
        # 対象期間に土曜が無い場合も**項は作る**（値は 0 になる）。
        # 作らないと「重みを上げても反応しない」係数が現れ、
        # 「重みが黙って無視されない」という保証が崩れる。
        # 空の袋で値がずれる問題は :func:`_fairness_spread` 側で
        # ``top == bottom`` として処理している。
        _fairness_spread(
            ctx,
            staff,
            periods,
            obj,
            saturday_weight,
            "sat",
            all_indices,
            saturday_days,
        )


def _fairness_spread(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    periods: Sequence[Sequence[date]],
    obj: list[object],
    weight: float,
    key: str,
    indices: Sequence[int],
    day_filter: set[date] | None,
) -> None:
    """ある区分の週レンジを最小化する項を ``obj`` に追加する。

    :param day_filter: 対象日に絞る場合の日の集合（``None`` で全日）。
        土曜出勤は「その日の出勤があるかどうか」で判定するため、全時間帯を指定した上で
        土曜日のみを対象にする。
    """
    placeable = [m for m in staff if m.is_placeable]
    if not placeable:
        return
    days = sorted({d for window in periods for d in window})
    if day_filter is not None:
        days = [d for d in days if d in day_filter]
    flags: dict[tuple[str, date], object] = {}
    for st in placeable:
        for day in days:
            day_var = ctx.day_var.get((st.staff_id, day), 0)
            flags[(st.staff_id, day)] = _fairness_day_flag(ctx, st, day, day_var, indices, key)
    for w, window in enumerate(periods):
        items: list[tuple[object, float]] = []
        for st in placeable:
            terms = [
                (flags[(st.staff_id, day)], 1)
                for day in window
                if (st.staff_id, day) in flags and not _is_zero(flags[(st.staff_id, day)])
            ]
            if terms:
                items.append((_linear(terms), 1))
        if not items:
            # 対象者が 1 人もいない袋では、項を**0 に固定**する。
            #
            # 何も制約を付けると CBC は ``top=0, bottom=cap`` を選び、
            # ``top - bottom = -cap``（= ``-len(window)``）を定数として
            # 目的関数に組み込んでしまう。その結果「土曜の無い計画では
            # 1 袋ごとに目的関数が ``-fairness_saturday_penalty`` ずれる」
            # という、実行値を比較できなくなるズレが生まれる。
            #
            # ``top == bottom`` を明示すれば値は 0 になり、かつ変数は残るため
            # 「重みを上げると必ず反応する」（= 重みが黙って無視されない）という
            # 保証も保てる。項ごと ``continue`` するとこの保証を壊す。
            cap = max(1, len(window))
            top = pulp.LpVariable(f"fmax_{key}_{w}", lowBound=0, upBound=cap)
            bottom = pulp.LpVariable(f"fmin_{key}_{w}", lowBound=0, upBound=cap)
            ctx.prob += top == bottom, f"fmx_{key}_{w}_empty"
            obj.append(weight * (top - bottom))
            continue
        # 上限は「その週の日数」。これを上回る回数にはならないので有界にする。
        # ここを無制限にすると ``top - bottom`` が目的関数を -∞ まで下げてしまい、
        # CBC が「解なし」と誤判定する（回帰の実際例）。
        cap = max(1, len(window))
        top = pulp.LpVariable(f"fmax_{key}_{w}", lowBound=0, upBound=cap)
        bottom = pulp.LpVariable(f"fmin_{key}_{w}", lowBound=0, upBound=cap)
        # 制約名は必ず付ける（:func:`_fairness_day_flag` と同じ理由で
        # PuLP の自動採番だと MPS に Duplicate row が出て CBC がモデルを捨てる）。
        for i, (value, _coef) in enumerate(items):
            ctx.prob += top >= value, f"fmx_{key}_{w}_{i}"
            ctx.prob += bottom <= value, f"fmn_{key}_{w}_{i}"
        obj.append(weight * (top - bottom))


