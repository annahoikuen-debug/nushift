"""Modularized core solver package for NuShift."""

from __future__ import annotations

from shiftai.solver_core.context import (
    _constraint_specs,
    _ConstraintSpec,
    _hard_ge,
    _hard_le,
    _is_one,
    _is_var,
    _is_zero,
    _linear,
    _ModelCtx,
    _SolveInput,
    _split_penalty,
    _var_value,
)
from shiftai.solver_core.decoder import (
    _non_integral_variables,
    _round_to_integral,
    _unassigned_variables,
    decode_solution,
    verify_solution,
)
from shiftai.solver_core.diagnosis import (
    SupplyGap,
    _coverage_diagnosis,
    _shortfall_supply,
    _staffing_advice,
    shortfall_rows,
    supply_hours,
)
from shiftai.solver_core.engine import (
    _get_solver,
    apply_warm_start,
    available_solvers,
    is_highs_available,
)
from shiftai.solver_core.post_process import (
    SnapChange,
    describe_shift_pattern,
    pattern_breakdown,
    snap_to_patterns,
)
from shiftai.solver_core.repair import repair_shift_absence
from shiftai.solver_core.solve import (
    _build_problem,
    _run_solver,
    solve_shift,
)

__all__ = [
    "_ConstraintSpec",
    "_ModelCtx",
    "_SolveInput",
    "_build_problem",
    "_constraint_specs",
    "_coverage_diagnosis",
    "_get_solver",
    "_hard_ge",
    "_hard_le",
    "_is_one",
    "_is_var",
    "_is_zero",
    "_linear",
    "_non_integral_variables",
    "_round_to_integral",
    "_run_solver",
    "_shortfall_supply",
    "_split_penalty",
    "_staffing_advice",
    "_unassigned_variables",
    "_var_value",
    "apply_warm_start",
    "available_solvers",
    "decode_solution",
    "describe_shift_pattern",
    "is_highs_available",
    "pattern_breakdown",
    "repair_shift_absence",
    "shortfall_rows",
    "snap_to_patterns",
    "solve_shift",
    "supply_hours",
    "verify_solution",
    "SnapChange",
    "SupplyGap",
]

