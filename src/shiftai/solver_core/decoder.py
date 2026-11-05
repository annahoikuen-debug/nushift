"""Solution decoding and constraint verification routines."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from shiftai.domain import (
    CellState,
    ShiftAssignment,
    ShiftDay,
    Slot,
    StaffMember,
)
from shiftai.solver_core.context import (
    _constraint_specs,
    _ModelCtx,
    _var_value,
)


def _extract_vars(problem_vars: object) -> tuple[dict, dict]:
    """problem_vars または ctx から (work, brk) 辞書を取り出す。"""
    if isinstance(problem_vars, _ModelCtx):
        return problem_vars.work, problem_vars.brk
    if isinstance(problem_vars, tuple) and len(problem_vars) == 2:
        return problem_vars[0], problem_vars[1]
    if isinstance(problem_vars, dict):
        return problem_vars.get("work", {}), problem_vars.get("brk", {})
    return getattr(problem_vars, "work", {}), getattr(problem_vars, "brk", {})


def _unassigned_variables(ctx: _ModelCtx) -> int:
    """値が None のままの変数の数を返す。"""
    count = 0
    for v in ctx.prob.variables():
        if getattr(v, "varValue", None) is None:
            count += 1
    return count


def _non_integral_variables(ctx: _ModelCtx, tol: float = 1e-4) -> int:
    """0/1 判定で許容誤差を超える中間値を取っている変数の数を返す。"""
    count = 0
    for v in ctx.prob.variables():
        val = getattr(v, "varValue", None)
        if val is not None and getattr(v, "cat", None) in ("Binary", "Integer"):
            if abs(val - round(val)) > tol:
                count += 1
    return count


def _round_to_integral(ctx: _ModelCtx, tol: float = 1e-4) -> None:
    """整数変数の微小な浮動小数点誤差を丸める。"""
    for v in ctx.prob.variables():
        val = getattr(v, "varValue", None)
        if val is not None and getattr(v, "cat", None) in ("Binary", "Integer"):
            if abs(val - round(val)) <= tol:
                v.varValue = float(round(val))


def verify_solution(ctx: _ModelCtx, tol: float = 1e-4) -> list[str]:
    """数値的に確定したハード制約違反を列挙して返す。"""
    violations = []
    specs = _constraint_specs(ctx)
    for spec in specs:
        if spec.is_violated(tol):
            violations.append(spec.name)
    return violations


def decode_solution(
    problem_vars: object,
    staff: Sequence[StaffMember],
    days: Sequence[date],
    slots: Sequence[Slot],
) -> tuple[list[ShiftDay], list[ShiftAssignment]]:
    """変数値から 1日分のシフト表（ShiftDay）と平坦な代入リストを復元する。"""
    work, brk = _extract_vars(problem_vars)
    staff_ids = [s.staff_id for s in staff]
    day_map = {d: ShiftDay(day=d, assignments={}) for d in days}
    assignments: list[ShiftAssignment] = []

    for sid in staff_ids:
        for day in days:
            row: dict[str, CellState] = {}
            for i, slot in enumerate(slots):
                if _var_value(work.get((sid, day, i), 0)) >= 0.5:
                    state = CellState.WORK
                elif _var_value(brk.get((sid, day, i), 0)) >= 0.5:
                    state = CellState.BREAK
                else:
                    state = CellState.OFF
                row[slot.label] = state
                assignments.append(ShiftAssignment(sid, day, slot, state))
            day_map[day].assignments[sid] = row

    return [day_map[d] for d in days], assignments

