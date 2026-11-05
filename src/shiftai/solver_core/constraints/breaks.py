"""Break times, statutory rest intervals, and consecutive working days constraints."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import pulp

from shiftai.domain import (
    ObjectiveWeights,
    RequirementTable,
    StaffMember,
    weekly_windows,
)
from shiftai.solver_core.context import (
    _EPS,
    _daily_cap_minutes,
    _hard_le,
    _is_zero,
    _linear,
    _ModelCtx,
)


def _add_breaks(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
    break_minutes: int,
) -> None:
    """勤務日ごとに連続した休憩が取れるようにする（不足分はソフトペナルティ）。"""
    slots = tuple(requirements.slots)
    gran = max(1, int(requirements.granularity_min or slots[0].minutes))
    for st in staff:
        sid = st.staff_id
        cap_min = _daily_cap_minutes(st.contract)
        need_slots = min(max(1, -(-break_minutes // gran)), max(0, cap_min // gran))
        if need_slots <= 0:
            continue
        for day in requirements.all_days():
            tag = day.strftime("%m%d")
            yvar = ctx.day_var.get((sid, day))
            if yvar is None:
                continue
            items = [
                (ctx.brk[(sid, day, i)], 1)
                for i in range(len(slots))
                if not _is_zero(ctx.brk.get((sid, day, i), 0))
            ]
            if not items:
                continue
            deficit = pulp.LpVariable(f"brkdef_{sid}_{tag}", lowBound=0)
            ctx.prob += _linear(items) >= need_slots * yvar - deficit
            # 不足分は ``break_conflict_penalty`` ではなく
            # ``break_deficit_penalty`` で罰する。労働基準法第34条の休憩義務は
            # 現場運用上の都合（通勤ペナルティなど）より重い扱いにしておく。
            obj.append(weights.break_deficit_penalty * deficit)
            # 「勤務しなかった日の休憩」を禁ずる。これは休憩が労働時間である以上
            # 「出勤していないのに休憩している」状態も矛盾するため。
            # 入れないと時間帯が 1 個だけの日に休憩が置かれ、
            # 乃至その時間帯が無人になる（``test_時間帯1個の境界`` の回帰）。
            if len(items) <= 1:
                ctx.prob += _linear(items) <= yvar
            else:
                _hard_le(ctx, _linear(items), len(items) * yvar, f"brkneedwork_{sid}_{tag}")


def _add_rest(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
) -> None:
    """勤務間の最低休息時間を罰変数で扱う（ハードにすると解が消えるため）。"""
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    for a, b in zip(days, days[1:], strict=False):
        for st in staff:
            sid = st.staff_id
            gap = (b - a).days * 24 * 60 - slots[-1].end_minutes + slots[0].start_minutes
            if gap >= st.contract.min_rest_hours * 60 - _EPS:
                continue
            fin = ctx.work.get((sid, a, len(slots) - 1), 0)
            stt = ctx.work.get((sid, b, 0), 0)
            if _is_zero(fin) and _is_zero(stt):
                continue
            rv = pulp.LpVariable(
                f"rest_{sid}_{a.isoformat()}_{b.isoformat()}", lowBound=0, upBound=1
            )
            ctx.prob += rv >= fin + stt - 1
            ctx.prob += rv <= fin
            ctx.prob += rv <= stt
            obj.append(weights.rest_violation_penalty * rv)


def _add_consecutive(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
) -> None:
    """連続勤務日数と週の勤務日数をスライド窓の罰変数で制限する。"""
    days = requirements.all_days()
    for st in staff:
        sid = st.staff_id
        # ``max_consecutive_days == 0`` は domain.py で
        # 「上限なし」と定義されているため、制約を作らない。
        # 修正前: ``max(1, ...)`` で 0 が 1 に変換され、
        # 「上限なし」が「1 日まで」に変わっていた。
        maxc = int(st.contract.max_consecutive_days)
        if maxc > 0:
            width = maxc + 1
            z_of: dict[tuple[date, date], object] = {}
            for a, b in zip(days, days[1:], strict=False):
                y1 = ctx.day_var.get((sid, a))
                y2 = ctx.day_var.get((sid, b))
                if y1 is None or y2 is None:
                    continue
                z = pulp.LpVariable(
                    f"z_{sid}_{a.isoformat()}_{b.isoformat()}", lowBound=0, upBound=1
                )
                ctx.prob += z >= y1 + y2 - 1
                ctx.prob += z <= y1
                ctx.prob += z <= y2
                z_of[(a, b)] = z
            if z_of and len(days) >= width:
                for i in range(len(days) - width + 1):
                    window = days[i : i + width]
                    z_terms = [
                        (z_of[(a, b)], 1)
                        for a, b in zip(window, window[1:], strict=False)
                        if (a, b) in z_of
                    ]
                    if not z_terms:
                        continue
                    slack = pulp.LpVariable(f"cons_{sid}_{i}", lowBound=0)
                    ctx.prob += _linear(z_terms) <= maxc - 1 + slack
                    obj.append(weights.consecutive_day_penalty * slack)
        if st.contract.max_weekly_days > 0:
            cap = st.contract.max_weekly_days
            for wi, window in enumerate(weekly_windows(days)):
                y_items = [(ctx.day_var[(sid, d)], 1) for d in window if (sid, d) in ctx.day_var]
                if y_items:
                    slack = pulp.LpVariable(f"weekly_{sid}_{wi}", lowBound=0)
                    ctx.prob += _linear(y_items) <= cap + slack
                    obj.append(weights.consecutive_day_penalty * slack)


