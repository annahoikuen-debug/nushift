"""週次シフト求解の薄いラッパー。

``solve_shift`` と同シグネチャ + ``engine`` / ``parallel_workers`` を持つ。
``engine="hierarchical"``（既定）の場合は ``solve_monthly_hierarchical`` に
日単位チャンク（chunk_size=1）で委譲する。高度機能（pairing_rules / classrooms /
require_leader / require_opening_closing_skill / drop_groups / relaxation != STRICT）
を使用する場合、または ``engine="legacy"`` の場合は ``solve_shift`` に
フォールバックする（既存の高度機能を維持するため）。
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import date

from shiftai import config
from shiftai.domain import (
    CellState,
    ChildPlan,
    ClassRoom,
    FacilitySettings,
    ObjectiveWeights,
    PairingRule,
    RequirementTable,
    SolveResult,
    StaffMember,
    StaffPreferences,
    StaffingStandard,
)
from shiftai.relaxation import RelaxLevel
from shiftai.shift_patterns import ShiftPattern


def solve_weekly(
    children: Sequence[ChildPlan],
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    weights: ObjectiveWeights | None = None,
    *,
    fixed_assignments: Mapping[tuple[str, date, str], CellState] | None = None,
    settings: FacilitySettings | None = None,
    time_limit_sec: int = config.DEFAULT_TIME_LIMIT_SEC,
    msg: bool = False,
    standard: StaffingStandard | None = None,
    patterns: Sequence[ShiftPattern] | None = None,
    relaxation: int = int(RelaxLevel.STRICT),
    drop_groups: frozenset[str] = frozenset(),
    solver_name: str = config.DEFAULT_SOLVER,
    warm_start: bool = True,
    warm_start_assignments: Mapping[tuple[str, date, str], CellState] | None = None,
    pattern_mode: bool = True,
    pairing_rules: Sequence[PairingRule] | None = None,
    classrooms: Sequence[ClassRoom] | None = None,
    require_leader: bool = False,
    require_opening_closing_skill: bool = False,
    engine: str = config.WEEKLY_ENGINE_DEFAULT,
    parallel_workers: int = config.WEEKLY_PARALLEL_WORKERS,
) -> SolveResult:
    """週次シフトを求解する。

    ``engine="hierarchical"``（既定）: ``solve_monthly_hierarchical`` に
    日単位チャンク（``config.WEEKLY_CHUNK_SIZE``）で委譲。
    ``engine="legacy"`` または高度機能使用時: ``solve_shift`` にフォールバック。

    注意:
    - hierarchical エンジンでは ``warm_start_assignments`` と ``msg`` は
      使用されない（Stage 1 が独自の warm start を生成するため）。
    - hierarchical 委譲時の ``target_month`` は対象期間の先頭日の月になる
      （月跨ぎの週では第 2 週以降の日付も先頭日の月名に含まれる）。
    - ``parallel_workers`` は 0 以下で「自動（CPU 数 - 1、最低 1）」になる。
    """
    # 高度機能・明示指定・空データ時は solve_shift にフォールバック
    has_advanced = bool(
        pairing_rules
        or classrooms
        or require_leader
        or require_opening_closing_skill
        or drop_groups
        or (relaxation != int(RelaxLevel.STRICT))
    )
    if parallel_workers <= 0:
        # 0=自動（CPU 数 - 1、最低 1）。config.WEEKLY_PARALLEL_WORKERS の既定値。
        parallel_workers = max(1, (os.cpu_count() or 2) - 1)
    preferences = dict(preferences or {})
    empty_input = not list(requirements.all_days()) or not staff
    if engine == "legacy" or has_advanced or empty_input:
        from shiftai.solver import solve_shift
        return solve_shift(
            children,
            staff,
            requirements,
            preferences,
            weights,
            fixed_assignments=fixed_assignments,
            settings=settings,
            time_limit_sec=time_limit_sec,
            msg=msg,
            standard=standard,
            patterns=patterns,
            relaxation=relaxation,
            drop_groups=drop_groups,
            solver_name=solver_name,
            warm_start=warm_start,
            warm_start_assignments=warm_start_assignments,
            pattern_mode=pattern_mode,
            pairing_rules=pairing_rules,
            classrooms=classrooms,
            require_leader=require_leader,
            require_opening_closing_skill=require_opening_closing_skill,
        )

    # hierarchical エンジン: 月次階層エンジンに日単位チャンクで委譲
    from shiftai.monthly import MonthlySolveContext, solve_monthly_hierarchical

    all_days = list(requirements.all_days())
    contracts_map = {s.staff_id: s.contract for s in staff if s.contract}
    ctx = MonthlySolveContext(
        target_month=f"{all_days[0].year:04d}-{all_days[0].month:02d}" if all_days else "",
        all_days=all_days,
        staff=staff,
        contracts=contracts_map,
        preferences=preferences,
        patterns=list(patterns or ()),
        requirements=requirements,
        facility_settings=settings or FacilitySettings(),
        fixed_assignments=fixed_assignments or {},
        prior_duty_history={},
        monthly_holiday_target=8,
        children=children,
        standard=standard,
        weights=weights,
    )
    return solve_monthly_hierarchical(
        ctx,
        time_limit_sec=time_limit_sec,
        solver_name=solver_name,
        parallel_workers=parallel_workers,
        chunk_size=config.WEEKLY_CHUNK_SIZE,
    )
