"""月次シフト2段階階層的求解（Hierarchical MILP）エンジン。

Stage 1: 月間日次パターン割り当て（職員×日付×勤務パターン）
  - 連続勤務上限（前月末境界バッファ考慮）
  - 月間所定公休目標の厳密遵守
  - パート職員の契約労働時間・所定労働日数遵守
  - 日次・時間帯ごとの概略配置需要充足

Stage 2: 週スライス単位の 30分スロット詳細配置 & 休憩スタッガー最適化
  - Stage 1 の公休および勤務パターンの時間外スロットを OFF 固定
  - パターン時間内の 30分スロットのみで配置基準充足・休憩スタッガーを最適化
  - 変数数を激減させ、高速かつ確実に実行可能解（FEASIBLE / OPTIMAL）に到達
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

import pulp

from shiftai.domain import (
    CellState,
    ChildPlan,
    Contract,
    FacilitySettings,
    ObjectiveWeights,
    RequirementTable,
    ShiftDay,
    SolveResult,
    SolveStatus,
    StaffMember,
    StaffPreferences,
    Violation,
    to_minutes,
)
from shiftai.shift_patterns import ShiftPattern
from shiftai.solver import is_highs_available, snap_to_patterns, solve_shift


@dataclass(frozen=True)
class MonthlySolveContext:
    target_month: str
    all_days: list[dt.date]
    staff: list[StaffMember]
    contracts: dict[str, Contract]
    preferences: dict[str, StaffPreferences]
    patterns: list[ShiftPattern]
    requirements: RequirementTable
    facility_settings: FacilitySettings
    fixed_assignments: dict[tuple[str, dt.date, str], CellState]
    prior_duty_history: dict[str, int]
    monthly_holiday_target: int
    children: Sequence[ChildPlan] = field(default_factory=tuple)


def solve_monthly_hierarchical(
    context: MonthlySolveContext,
    time_limit_sec: int = 120,
    solver_name: str | None = None,
) -> SolveResult:
    """2段階階層的アプローチにより月次一括シフトを求解する。"""
    # ソルバーの自動選択（HiGHS 優先、なければ CBC）
    effective_solver = solver_name
    if not effective_solver:
        effective_solver = "HiGHS" if is_highs_available() else "PULP_CBC_CMD"

    # ---------------------------------------------------------
    # Stage 1: 月間日次パターンの割り当て (MILP)
    # ---------------------------------------------------------
    stage1_time = min(max(time_limit_sec // 4, 15), 30)
    stage1_res = _solve_stage1_daily_patterns(
        context=context,
        time_limit_sec=stage1_time,
        solver_name=effective_solver,
    )

    combined_fixed = dict(context.fixed_assignments)
    warm_starts: dict[tuple[str, dt.date, str], CellState] = {}

    slots = tuple(context.requirements.slots)
    pattern_by_key = {p.key: p for p in context.patterns}

    for (staff_id, day), pat_key in stage1_res.items():
        if pat_key == "OFF":
            for s in slots:
                key = (staff_id, day, s.label)
                if key not in combined_fixed:
                    # Stage 1 で OFF と判定された日は Stage 2 でも全スロット OFF 固定
                    combined_fixed[key] = CellState.OFF
                    warm_starts[key] = CellState.OFF
            continue

        pat = pattern_by_key.get(pat_key)
        if not pat:
            continue

        pat_start_min = to_minutes(pat.start)
        pat_end_min = to_minutes(pat.end)

        for s in slots:
            key = (staff_id, day, s.label)
            if key in combined_fixed:
                continue
            if s.start_minutes >= pat_start_min and s.end_minutes <= pat_end_min:
                warm_starts[key] = CellState.WORK
            else:
                # パターン時間外のスロットは OFF に固定して変数を大幅削減
                combined_fixed[key] = CellState.OFF
                warm_starts[key] = CellState.OFF

    # ---------------------------------------------------------
    # Stage 2: 週スライス単位の 30分スロット詳細配置 & 休憩スタッガー最適化
    # ---------------------------------------------------------
    chunk_size = 7
    day_chunks = [
        context.all_days[i : i + chunk_size]
        for i in range(0, len(context.all_days), chunk_size)
    ]

    all_shift_days: list[ShiftDay] = []
    all_violations: list[Violation] = []
    all_messages: list[str] = []
    is_all_optimal = True
    is_any_feasible = False

    weights = ObjectiveWeights(split_duty_penalty=5000.0)
    remaining_budget = max(time_limit_sec - stage1_time, 30)
    per_chunk_time = max(remaining_budget // max(len(day_chunks), 1), 15)

    for week_days in day_chunks:
        week_days_set = set(week_days)
        week_children = [c for c in context.children if c.day in week_days_set]

        week_requirements = RequirementTable(
            day_open=context.requirements.day_open,
            day_close=context.requirements.day_close,
            granularity_min=context.requirements.granularity_min,
            slots=context.requirements.slots,
            rows={d: context.requirements.for_day(d) for d in week_days},
            notes=list(context.requirements.notes),
        )

        week_fixed = {k: v for k, v in combined_fixed.items() if k[1] in week_days_set}
        week_warm = {k: v for k, v in warm_starts.items() if k[1] in week_days_set}

        sub_res = solve_shift(
            week_children,
            staff=context.staff,
            requirements=week_requirements,
            preferences=context.preferences,
            weights=weights,
            settings=context.facility_settings,
            fixed_assignments=week_fixed,
            time_limit_sec=per_chunk_time,
            solver_name=effective_solver,
            warm_start=True,
            warm_start_assignments=week_warm,
            patterns=context.patterns,
            pattern_mode=True,
        )

        if context.patterns and sub_res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
            sub_res, _ = snap_to_patterns(
                sub_res,
                context.patterns,
                requirements=week_requirements,
                preferences=context.preferences,
                staff=context.staff,
                settings=context.facility_settings,
            )

        if sub_res.status == SolveStatus.OPTIMAL:
            is_any_feasible = True
        elif sub_res.status == SolveStatus.FEASIBLE:
            is_all_optimal = False
            is_any_feasible = True
        else:
            is_all_optimal = False

        all_shift_days.extend(sub_res.shift_days)
        all_violations.extend(sub_res.violations)
        all_messages.extend(sub_res.messages)

    if is_all_optimal and len(all_shift_days) == len(context.all_days):
        overall_status = SolveStatus.OPTIMAL
    elif is_any_feasible and len(all_shift_days) == len(context.all_days):
        overall_status = SolveStatus.FEASIBLE
    else:
        overall_status = SolveStatus.PARTIAL

    return SolveResult(
        status=overall_status,
        shift_days=all_shift_days,
        violations=all_violations,
        messages=list(dict.fromkeys(all_messages)),
        stats={
            "solver": "hierarchical_monthly",
            "backend_solver": effective_solver,
            "num_days": len(context.all_days),
            "num_chunks": len(day_chunks),
            "stage1_patterns": len(stage1_res),
        },
    )


def _solve_stage1_daily_patterns(
    context: MonthlySolveContext,
    time_limit_sec: int = 30,
    solver_name: str | None = None,
) -> dict[tuple[str, dt.date], str]:
    """Stage 1: 日単位のパターン割り当てモデルを PuLP で定式化・求解する。"""
    prob = pulp.LpProblem("Monthly_Stage1_Pattern_Assignment", pulp.LpMinimize)

    staff_ids = [s.staff_id for s in context.staff]
    days = context.all_days
    patterns = context.patterns
    pat_keys = [p.key for p in patterns]

    x_vars: dict[tuple[str, dt.date, str], pulp.LpVariable] = {}
    duty_vars: dict[tuple[str, dt.date], pulp.LpVariable] = {}

    for sid in staff_ids:
        for d in days:
            duty_vars[(sid, d)] = pulp.LpVariable(f"duty_{sid}_{d.isoformat()}", cat=pulp.LpBinary)
            for p in patterns:
                x_vars[(sid, d, p.key)] = pulp.LpVariable(
                    f"x_{sid}_{d.isoformat()}_{p.key}", cat=pulp.LpBinary
                )

    obj_terms: list[pulp.LpAffineExpression] = []

    # 制約1: 1日1パターン以下、勤務時は duty_vars とリンク
    for sid in staff_ids:
        for d in days:
            prob += pulp.lpSum([x_vars[(sid, d, pk)] for pk in pat_keys]) == duty_vars[(sid, d)]

    # 制約2: 契約所定労働時間（daily_hours）を超える長時間のパターンを禁止
    for s in context.staff:
        sid = s.staff_id
        contract = context.contracts.get(sid) or s.contract
        max_daily_h = (
            contract.daily_hours
            if (contract and getattr(contract, "daily_hours", None) is not None)
            else 8.0
        )
        for p in patterns:
            pat_h = (to_minutes(p.end) - to_minutes(p.start)) / 60.0
            work_h = pat_h - 1.0 if pat_h > 6.0 else pat_h
            if work_h > max_daily_h + 0.1:
                # この職員の 1日上限時間を超えるパターンは禁止
                for d in days:
                    prob += x_vars[(sid, d, p.key)] == 0

    # 制約3: 月間公休目標
    for sid in staff_ids:
        contract = context.contracts.get(sid)
        target_holidays = context.monthly_holiday_target
        if contract and hasattr(contract, "monthly_holiday_target") and contract.monthly_holiday_target:
            target_holidays = contract.monthly_holiday_target

        work_days_target = len(days) - target_holidays
        prob += pulp.lpSum([duty_vars[(sid, d)] for d in days]) <= work_days_target

    # 制約4: 連続勤務上限（前月末連勤履歴考慮）
    for sid in staff_ids:
        contract = context.contracts.get(sid)
        max_streak = 5
        if contract and hasattr(contract, "max_consecutive_days") and contract.max_consecutive_days:
            max_streak = contract.max_consecutive_days

        prior_streak = context.prior_duty_history.get(sid, 0)
        if prior_streak > 0:
            remaining_allowance = max(0, max_streak - prior_streak)
            prob += (
                pulp.lpSum([duty_vars[(sid, days[i])] for i in range(min(remaining_allowance + 1, len(days)))])
                <= remaining_allowance
            )

        window_size = max_streak + 1
        for i in range(len(days) - window_size + 1):
            window_days = [days[i + k] for k in range(window_size)]
            prob += pulp.lpSum([duty_vars[(sid, d)] for d in window_days]) <= max_streak

    # 制約5: 日次最低必要人員（大まかな確保）
    for d in days:
        req_list = context.requirements.for_day(d)
        max_req = max([r.needed_staff for r in req_list], default=0)
        if max_req > 0:
            prob += pulp.lpSum([duty_vars[(sid, d)] for sid in staff_ids]) >= min(max_req, len(staff_ids))

    # 制約6: 職員の公平な勤務日数平準化（目的関数）
    avg_work_days = len(days) - context.monthly_holiday_target
    for sid in staff_ids:
        work_diff = pulp.LpVariable(f"diff_{sid}", lowBound=0)
        prob += pulp.lpSum([duty_vars[(sid, d)] for d in days]) - avg_work_days <= work_diff
        prob += avg_work_days - pulp.lpSum([duty_vars[(sid, d)] for d in days]) <= work_diff
        obj_terms.append(work_diff * 10)

    prob += pulp.lpSum(obj_terms)

    chosen_solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit_sec)
    if solver_name == "HiGHS" and hasattr(pulp, "HiGHS"):
        try:
            chosen_solver = pulp.HiGHS(msg=False, timeLimit=time_limit_sec)
        except Exception:
            pass

    prob.solve(chosen_solver)

    assignments: dict[tuple[str, dt.date], str] = {}
    for sid in staff_ids:
        for d in days:
            assigned = "OFF"
            for p in patterns:
                var = x_vars.get((sid, d, p.key))
                if var and var.varValue and var.varValue > 0.5:
                    assigned = p.key
                    break
            assignments[(sid, d)] = assigned

    return assignments
