"""Pattern alignment objective penalties and hard pattern mode constraints."""

from __future__ import annotations

from collections.abc import Sequence

import pulp

from shiftai.domain import ObjectiveWeights, RequirementTable, StaffMember
from shiftai.shift_patterns import ShiftPattern
from shiftai.solver_core.context import (
    _is_zero,
    _linear,
    _ModelCtx,
)


def _pattern_alignment_penalty(weights: ObjectiveWeights) -> float:
    """勤務ブロックの整列（スナップ）ペナルティ。

    ``ObjectiveWeights`` に専用項目を設けない方針を保ち、他の重みから導出する。
    既定値では 1 日の長時間勤務の内部目安（6.0）より安く、
    配置基準 1 名不足（1000.0）よりはるかに安く着く。
    （境界が 1 時間帯ずれるくらいなら、人を欠けるよりはマシだと現場が判断する。）
    """
    return max(5.0 * weights.max_shift_length_penalty, 1.0)


def _add_pattern_alignment(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
    patterns: Sequence[ShiftPattern],
    *,
    pattern_mode: bool = False,
) -> None:
    """現場固有の勤務パターン（早番・日勤・遅番）への整列。

    pattern_mode=True の場合は出勤時にいずれかのパターンへ厳密に一致させる（ハード制約）。
    False の場合は目的関数で境界を引き寄せる（ソフトペナルティ）。
    """
    slots = tuple(requirements.slots)
    if not slots or not patterns:
        return
    penalty = _pattern_alignment_penalty(weights)
    if penalty <= 0 and not pattern_mode:
        return
    ranges: list[list[int]] = []
    for pattern in patterns:
        picked = [
            i
            for i, slot in enumerate(slots)
            if slot.start_minutes >= pattern.start_minutes
            and slot.end_minutes <= pattern.end_minutes
        ]
        if picked and picked[-1] - picked[0] + 1 == len(picked):
            ranges.append(picked)
    if not ranges:
        return
    days = requirements.all_days()
    for st in staff:
        sid = st.staff_id
        for day in days:
            duty: dict[int, object] = {}
            for i in range(len(slots)):
                wv = ctx.work.get((sid, day, i), 0)
                bv = ctx.brk.get((sid, day, i), 0)
                if _is_zero(wv) and _is_zero(bv):
                    continue
                duty[i] = wv + bv
            if not duty:
                continue
            tag = day.strftime("%m%d")
            matched: list[object] = []
            for k, indices in enumerate(ranges):
                var = pulp.LpVariable(f"pat_{sid}_{tag}_{k}", lowBound=0, upBound=1)
                terms: list[object] = []
                for i in indices:
                    cell = duty.get(i, 0)
                    ctx.prob += var >= cell - 1
                    if not _is_zero(cell):
                        terms.append(cell)
                ctx.prob += len(indices) * var <= _linear([(t, 1) for t in terms])
                matched.append(var)
                if penalty > 0:
                    obj.append(-penalty * var)
            if len(matched) > 1:
                ctx.prob += _linear([(v, 1) for v in matched]) <= 1
            if pattern_mode and matched:
                dvar = ctx.day_var.get((sid, day))
                if dvar is not None and not _is_zero(dvar):
                    ctx.prob += pulp.lpSum(matched) == dvar, f"pat_exact_{sid}_{tag}"
                for i, cell in duty.items():
                    if _is_zero(cell):
                        continue
                    covering = [matched[k] for k, indices in enumerate(ranges) if i in indices]
                    if covering:
                        ctx.prob += cell <= pulp.lpSum(covering), f"pat_cover_{sid}_{tag}_{i}"
                    else:
                        ctx.prob += cell == 0, f"pat_no_pattern_{sid}_{tag}_{i}"


