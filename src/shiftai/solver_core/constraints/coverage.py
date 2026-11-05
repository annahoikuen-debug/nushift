"""Staff coverage and daily cell decision variables."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import pulp

from shiftai import config
from shiftai.config import STATUTORY_DAILY_WORK_HOURS
from shiftai.domain import (
    CellState,
    FacilitySettings,
    ObjectiveWeights,
    RequirementTable,
    Role,
    Slot,
    SlotKind,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
)
from shiftai.solver_core.constraints.blocks import (
    _add_duty_block,
    _commute_reach_slots,
)
from shiftai.solver_core.context import (
    _BREAK_CONCURRENT_SHARE,
    _daily_cap_minutes,
    _day_is_workable,
    _hard_ge,
    _hard_le,
    _is_unavailable,
    _is_zero,
    _linear,
    _ModelCtx,
    _slot_is_contractible,
)


def _build_cells(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    prefs: Mapping[str, StaffPreferences],
    fixed: Mapping[tuple[str, date, str], CellState],
    settings: FacilitySettings,
    kinds: Sequence[SlotKind],
    obj: list[object],
    weights: ObjectiveWeights,
    ignore_unavailable: bool = False,
    drop_groups: frozenset[str] = frozenset(),
    allow_split: bool = False,
) -> None:
    """セル変数の生成と、セル単位のハード制約（排他・在勤/休憩ブロック・1日上限）。

    :param allow_split: ``True`` なら「在勤1日1ブロック・休息1日1回」を
        ソフトペナルティに戻す（``relaxation=RELAX_HOURS`` で有効）。
    """
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    n = len(slots)
    standby = {d for d in days if not requirements.for_day(d)}
    if "fixed" in drop_groups:
        fixed = {}
    for st in staff:
        sid = st.staff_id
        prefs_st = None if ignore_unavailable or "unavailable" in drop_groups else prefs.get(sid)
        cap_min = _daily_cap_minutes(st.contract)
        if "daily_cap" in drop_groups:
            cap_min = max(cap_min, STATUTORY_DAILY_WORK_HOURS * 60)
        hours = pulp.LpVariable(f"H_{sid}", lowBound=0)
        ctx.hours[sid] = hours
        week_items: list[tuple[object, float]] = []
        # 週ごとの勤務時間（「週所定 44 時間」の判定に使う）。
        # ``hours`` は計画全体の合計なので、週の上限と比較できない。
        for day in days:
            tag = day.strftime("%m%d")
            day_ok = _day_is_workable(st, day, settings) and day not in standby
            if ignore_unavailable or "closed" in drop_groups:
                day_ok = day not in standby
            if day in standby:
                for i in range(len(slots)):
                    ctx.work[(sid, day, i)] = 0
                    ctx.brk[(sid, day, i)] = 0
                    ctx.day_work[(sid, day)] = 0
                continue
            yvar = pulp.LpVariable(f"yd_{sid}_{tag}", 0, 1, pulp.LpBinary)
            ctx.day_var[(sid, day)] = yvar
            w_cells: dict[int, object] = {}
            b_cells: dict[int, object] = {}
            day_items: list[tuple[object, float]] = []
            for i, slot in enumerate(slots):
                key = (sid, day, i)
                forced = fixed.get((sid, day, slot.label))
                allowed = day_ok and ("contract" in drop_groups or _slot_is_contractible(st, slot)) and st.is_placeable
                if allowed and _is_unavailable(prefs_st, day, slot):
                    allowed = False
                if forced is CellState.WORK:
                    w_cells[i] = 1
                    b_cells[i] = 0
                    # 定数化して最適化から外しても、デコードの復元には必要なので
                    # ctx にも「定数としての値」を残す。以前はここへの記録が
                    # 無く、モデルは固定セルを尊重しているのに出力だけ OFF になっていた。
                    ctx.work[key] = 1
                    ctx.brk[key] = 0
                    day_items.append((1, slot.minutes))
                    week_items.append((1, slot.hours))
                    continue
                if forced is CellState.BREAK:
                    w_cells[i] = 0
                    b_cells[i] = 1
                    ctx.work[key] = 0
                    ctx.brk[key] = 1
                    continue
                if forced is not None or not allowed:
                    w_cells[i] = 0
                    b_cells[i] = 0
                    ctx.work[key] = 0
                    ctx.brk[key] = 0
                    continue
                wvar = pulp.LpVariable(f"w_{sid}_{tag}_{i}", 0, 1, pulp.LpBinary)
                bvar = pulp.LpVariable(f"b_{sid}_{tag}_{i}", 0, 1, pulp.LpBinary)
                ctx.work[key] = wvar
                ctx.brk[key] = bvar
                w_cells[i] = wvar
                b_cells[i] = bvar
                ctx.prob += wvar + bvar <= 1
                day_items.append((wvar, slot.minutes))
                week_items.append((wvar, slot.hours))
                # 「週 44 時間」判定用に、週ごとの勤務時間を分ける。
                # 計画全体の合計（``hvar``）は週の上限と比較できないため。
                if kinds[i] is SlotKind.EARLY:
                    obj.append(weights.early_shift_penalty * wvar)
                elif kinds[i] is SlotKind.LATE or kinds[i] is SlotKind.LATE_STRICT:
                    obj.append(weights.late_shift_penalty * wvar)

            duty: dict[int, object] = {}
            work_map: dict[int, object] = {}
            breaks: dict[int, object] = {}
            for i in range(n):
                wv = w_cells[i]
                bv = b_cells[i]
                if _is_zero(wv) and _is_zero(bv):
                    continue
                duty[i] = wv + bv
                if not _is_zero(wv):
                    work_map[i] = wv
                if not _is_zero(bv):
                    breaks[i] = bv
            if "duty_block" not in drop_groups:
                # 案1: 1職員1日の在勤は1連続ブロック・休息は1回だけにする。
                # allow_split=True（緩和 L3 ``RELAX_HOURS``）ならソフトに戻る。
                _add_duty_block(
                    ctx,
                    duty,
                    work_map,
                    breaks,
                    f"{sid}_{tag}",
                    n,
                    allow_split=allow_split or config.ALLOW_DUTY_BLOCK_SPLIT,
                    commute_reach=_commute_reach_slots(n),
                    obj=obj,
                    weights=weights,
                )

            if "daily_cap" not in drop_groups:
                _hard_le(
                    ctx,
                    _linear(day_items),
                    cap_min,
                    f"dailycap_{sid}_{day.isoformat()}",
                )
            ctx.day_work[(sid, day)] = _linear(day_items)
            w_only = [w_cells[i] for i in range(n) if not _is_zero(w_cells[i])]
            if w_only:
                # PuLP の LpAffineExpression は int による除算持っていないため、
                # 「n で割る」を「1/n 倍する」で書く（時間帯 1 個の問題が落ちるのを防ぐ）。
                # 割る分母は **時間帯総数 n ではなく、実際に勤務変数がある
                # 時間帯数 ``len(w_only)``** でなければならない。
                # 契約時間帯の外側（最早始業より前など）は ``w_cells[i] == 0``
                # になるため、n で割ると 1 個だけ働いた日が ``1/n``（=0.06 程度）
                # のまま取り残り、勤務変数が分数になる。すると
                # ``y`` を「その日働いたか 0/1 のフラグ」として使う
                # 連続勤務日数・週所定出勤日数・休憩時間の各制約が
                # まとめて弱まってしまう（``:func:`_fairness_day_flag`` が
                # ``1.0 / len(items)`` としているのと同じ不整合）。
                _hard_ge(ctx, yvar, _linear([(v, 1) for v in w_only]) * (1.0 / len(w_only)))
                _hard_le(ctx, yvar, _linear([(v, 1) for v in w_only]))
        ctx.prob += hours == _linear(week_items)


def _qualified_items(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    day: date,
    idx: int,
    standard: StaffingStandard | None,
) -> list[object]:
    """「必要保育士数」を満たす勤務変数を返す。

    看護師・准看護師を保育士とみなせる基準
    （``nurse_as_qualified_cap`` が 1 以上。企業主導型保育事業など）は看護師も含める。
    ただし**上限を超える複数の看護師は同時に数えられない**ため、
    看護師が上限より多いときだけ 0/1 変数を 1 つ追加して頭数を絞る。
    通常（看護師が 1 名以下の園では）変数は増えない。
    """
    nurse_cap = standard.nurse_as_qualified_cap if standard is not None else 0
    items: list[object] = []
    nurse_items: list[object] = []
    for member in staff:
        var = ctx.work.get((member.staff_id, day, idx))
        if var is None or _is_zero(var):
            continue
        if member.has_role(Role.HOIKUSHI):
            items.append(var)
        elif nurse_cap > 0 and member.is_nurse:
            nurse_items.append(var)
    if not nurse_items:
        return items
    if nurse_cap >= len(nurse_items):
        return items + nurse_items

    # 看護師が上限より多いときだけ「1 人分だけ数える」変数を導入する。
    used = pulp.LpVariable(f"nurse_used_{day.isoformat()}_{ctx.prob.name}_{idx}", cat=pulp.LpBinary)
    for var in nurse_items:
        ctx.prob += used >= var
    ctx.prob += used <= _linear([(v, 1) for v in nurse_items])
    return items + [used]


def _add_coverage(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object] | None = None,
    weights: ObjectiveWeights | None = None,
    standard: StaffingStandard | None = None,
) -> None:
    """配置基準（is_binding=True の行）を課す。

    ``obj`` と ``weights`` を渡すと「ハード制約」ではなく「不足人数 ×
    shortfall_penalty の罰変数」になる。1パス目で実行可能解が得られなかった場合の
    2パス目（ベストエフォート用）で使う。``needed_qualified`` と ``needed_staff``
    は行ごとに独立して合計し、2名ルールで底上げされた行もそのまま扱う。
    """
    soft = obj is not None and weights is not None
    slots = tuple(requirements.slots)
    index_of = {slot: i for i, slot in enumerate(slots)}
    for day in requirements.all_days():
        by_slot: dict[Slot, list] = {}
        for row in requirements.for_day(day):
            if not row.is_binding:
                continue
            by_slot.setdefault(row.slot, []).append(row)
        for slot, rows in by_slot.items():
            idx = index_of.get(slot)
            if idx is None:
                continue
            need_all = sum(r.needed_staff for r in rows)
            need_q = sum(r.needed_qualified for r in rows)
            all_items = [
                ctx.work[(s.staff_id, day, idx)]
                for s in staff
                if (s.staff_id, day, idx) in ctx.work
            ]
            q_items = _qualified_items(ctx, staff, day, idx, standard)
            plans = [(need_q, q_items, "必要保育士", "coverq")]
            if need_all > need_q:
                plans.append((need_all, all_items, "必要人員", "cover"))
            for need, items, label, code in plans:
                if need <= 0:
                    continue
                name = f"{code}_{day.isoformat()}_{slot.label}"
                if not items:
                    ctx.conflicts.append(
                        f"{day.isoformat()} {slot.label} の{label} {need} 名を配置できる職員がいません"
                    )
                    continue
                expr = _linear([(v, 1) for v in items])
                if soft:
                    gap = pulp.LpVariable(f"gap_{name}", lowBound=0)
                    ctx.prob += expr + gap >= need
                    obj.append(weights.shortfall_penalty * gap)
                    ctx.shortfall[name] = (gap, need, day, slot, label)
                else:
                    _hard_ge(ctx, expr, need, name)


def _add_workload(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
) -> None:
    """超過配置・休憩不足・休憩集中のソフトペナルティ。"""
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    index_of = {slot: i for i, slot in enumerate(slots)}
    n = len(slots)
    for day in days:
        tag = day.strftime("%m%d")
        need_by_slot: dict[int, int] = {}
        for row in requirements.for_day(day):
            if row.is_binding:
                need_by_slot[index_of.get(row.slot, -1)] = (
                    need_by_slot.get(index_of.get(row.slot, -1), 0) + row.needed_staff
                )
        for idx, need_all in need_by_slot.items():
            if idx < 0 or need_all <= 0:
                continue
            items = [
                (ctx.work[(s.staff_id, day, idx)], 1)
                for s in staff
                if (s.staff_id, day, idx) in ctx.work
                and not _is_zero(ctx.work[(s.staff_id, day, idx)])
            ]
            if not items:
                continue
            over = pulp.LpVariable(f"over_{tag}_{idx}", lowBound=0)
            ctx.prob += _linear(items) - need_all <= over
            obj.append(weights.overstaff_penalty * over)

        n_potential = sum(
            1
            for s in staff
            if any(not _is_zero(ctx.work.get((s.staff_id, day, i), 0)) for i in range(n))
        )
        if n_potential <= 0:
            continue
        cap = max(1, int(_BREAK_CONCURRENT_SHARE * n_potential))
        for i in range(n):
            items = [
                (ctx.brk[(s.staff_id, day, i)], 1)
                for s in staff
                if (s.staff_id, day, i) in ctx.brk and not _is_zero(ctx.brk[(s.staff_id, day, i)])
            ]
            if len(items) <= cap:
                continue
            excess = pulp.LpVariable(f"brkexc_{tag}_{i}", lowBound=0)
            ctx.prob += _linear(items) <= cap + excess
            obj.append(weights.break_conflict_penalty * excess)


