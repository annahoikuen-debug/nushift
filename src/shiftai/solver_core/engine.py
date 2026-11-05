"""MILP solver engine bindings, availability checks, and warm-start routines."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import pulp

from shiftai.domain import CellState, Slot
from shiftai.solver_core.context import _ModelCtx


def is_highs_available() -> bool:
    """HiGHS ソルバーが利用可能か判定する。"""
    if not hasattr(pulp, "HiGHS"):
        return False
    try:
        return bool(pulp.HiGHS(msg=False).available())
    except Exception:
        return False


def available_solvers() -> list[str]:
    """PuLP 経由で利用可能な MILP ソルバーの一覧を返す。"""
    try:
        solvers = list(pulp.listSolvers(onlyAvailable=True))
        if is_highs_available() and "HiGHS" not in solvers:
            solvers.append("HiGHS")
        return solvers
    except Exception:
        return []


def _get_solver(
    limit_sec: int,
    msg: bool,
    solver_name: str = "auto",
    warm_start: bool = False,
) -> tuple[pulp.LpSolver, str]:
    """指定または自動選択されたソルバーインスタンスとソルバー識別名を返す。"""
    limit = max(1, int(limit_sec))
    chosen = (solver_name or "auto").strip()
    if chosen == "auto":
        if is_highs_available():
            chosen = "HiGHS"
        else:
            chosen = "PULP_CBC_CMD"

    if chosen.lower() in ("highs", "highspy") and is_highs_available():
        try:
            return pulp.HiGHS(msg=msg, timeLimit=limit, warmStart=warm_start), "HiGHS"
        except Exception:
            pass

    return pulp.PULP_CBC_CMD(msg=msg, timeLimit=limit, warmStart=warm_start), "PULP_CBC_CMD"


def apply_warm_start(
    ctx: _ModelCtx,
    slots: Sequence[Slot],
    warm_assignments: Mapping[tuple[str, date, str], CellState] | Sequence[Any] | None = None,
) -> int:
    """決定変数に初期解（ウォームスタート値）を設定する。設定した変数数を返す。"""
    if not warm_assignments:
        return 0
    label_to_idx = {slot.label: i for i, slot in enumerate(slots)}
    count = 0

    if isinstance(warm_assignments, Mapping):
        items = list(warm_assignments.items())
    else:
        items = [((a.staff_id, a.day, a.slot.label), a.state) for a in warm_assignments]

    for (sid, day, slot_label), state in items:
        idx = label_to_idx.get(slot_label)
        if idx is None:
            continue
        w_var = ctx.work.get((sid, day, idx))
        b_var = ctx.brk.get((sid, day, idx))
        if w_var is not None and not isinstance(w_var, (int, float)):
            w_var.setInitialValue(1.0 if state is CellState.WORK else 0.0)
            count += 1
        if b_var is not None and not isinstance(b_var, (int, float)):
            b_var.setInitialValue(1.0 if state is CellState.BREAK else 0.0)
            count += 1
    return count


