"""Timefold 思想に基づく Ruin & Recreate 局所探索（Local Search）フォールバックエンジン。

MILP がタイムアウトした際、または Infeasible に陥った際、
Greedy 解や暫定解の違反箇所を局所的に破壊（Ruin）し、出勤可能な代替職員に
実務勤務パターン（ShiftPattern）を割り当て直す（Recreate）ことで、
中抜けや休憩違反を起こさず高速に実行可能解へ改善する。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from shiftai.domain import (
    CellState,
    FacilitySettings,
    RequirementTable,
    ShiftDay,
    SolveResult,
    SolveStatus,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    to_minutes,
)
from shiftai.gap_analysis import check_violations
from shiftai.score import compute_shift_score
from shiftai.shift_patterns import ShiftPattern, default_patterns


def ruin_and_recreate(
    initial_result: SolveResult,
    requirements: RequirementTable,
    staff: Sequence[StaffMember],
    preferences: Mapping[str, StaffPreferences] | None = None,
    standard: StaffingStandard | None = None,
    settings: FacilitySettings | None = None,
    fixed_assignments: Mapping[tuple[str, date, str], CellState] | None = None,
    patterns: Sequence[ShiftPattern] | None = None,
    max_iterations: int = 50,
) -> SolveResult:
    """パターンベースの Ruin & Recreate 局所探索により、暫定解の制約違反を順次解消・改善する。"""
    prefs = preferences or {}
    fixed = fixed_assignments or {}
    cand_patterns = list(patterns or default_patterns())
    slots = tuple(requirements.slots)
    placeable_staff = [s for s in staff if s.is_placeable]

    current_shift_days = [copy.deepcopy(sd) for sd in initial_result.shift_days]

    # 現在のスコアを算出
    current_violations = check_violations(
        requirements,
        initial_result,
        staff,
        prefs,
        standard=standard,
        settings=settings,
    )
    current_score = compute_shift_score(
        shift_days=current_shift_days,
        violations=current_violations,
        requirements=requirements,
        staff=staff,
        preferences=prefs,
    )

    best_shift_days = [copy.deepcopy(sd) for sd in current_shift_days]
    best_violations = list(current_violations)
    best_score = current_score

    improved = False

    for iteration in range(max_iterations):
        if best_score.hard_score == 0 and len(best_violations) == 0:
            break

        # 違反日の特定
        violated_dates = {v.day for v in best_violations if v.day}
        if not violated_dates:
            violated_dates = {sd.day for sd in best_shift_days}

        any_move = False

        for target_day in sorted(violated_dates):
            sd = next((d for d in best_shift_days if d.day == target_day), None)
            if not sd:
                continue

            # この日の非勤務（OFF）職員を探す
            off_staff = [
                s for s in placeable_staff
                if all(sd.assignments.get(s.staff_id, {}).get(slot.label, CellState.OFF) == CellState.OFF for slot in slots)
            ]

            for sm in off_staff:
                pref = prefs.get(sm.staff_id)
                # パターンを試行
                for pat in cand_patterns:
                    p_start = to_minutes(pat.start)
                    p_end = to_minutes(pat.end)

                    # 希望休に衝突していないか
                    conflict = False
                    for slot in slots:
                        if p_start <= slot.start_minutes and slot.end_minutes <= p_end:
                            if pref and pref.is_unavailable(target_day, slot):
                                conflict = True
                                break
                            if (sm.staff_id, target_day, slot.label) in fixed:
                                conflict = True
                                break
                    if conflict:
                        continue

                    # 候補シフトを作成（この職員にパターンを割り当て）
                    trial_shift_days = [copy.deepcopy(d) for d in best_shift_days]
                    trial_sd = next(d for d in trial_shift_days if d.day == target_day)

                    # パターン適用（6h超なら1h休憩）
                    span_m = p_end - p_start
                    has_break = span_m > 6 * 60
                    break_start = p_start + (span_m // 2) - 30

                    for slot in slots:
                        if p_start <= slot.start_minutes and slot.end_minutes <= p_end:
                            if has_break and (break_start <= slot.start_minutes < break_start + 60):
                                trial_sd.assignments.setdefault(sm.staff_id, {})[slot.label] = CellState.BREAK
                            else:
                                trial_sd.assignments.setdefault(sm.staff_id, {})[slot.label] = CellState.WORK

                    # 評価
                    trial_result = SolveResult(
                        status=SolveStatus.FEASIBLE,
                        shift_days=trial_shift_days,
                        violations=[],
                    )
                    trial_violations = check_violations(
                        requirements,
                        trial_result,
                        staff,
                        prefs,
                        standard=standard,
                        settings=settings,
                    )
                    trial_score = compute_shift_score(
                        shift_days=trial_shift_days,
                        violations=trial_violations,
                        requirements=requirements,
                        staff=staff,
                        preferences=prefs,
                    )

                    # スコア改善判定 (Hard優先)
                    is_better = False
                    if trial_score.hard_score > best_score.hard_score:
                        is_better = True
                    elif trial_score.hard_score == best_score.hard_score and trial_score.soft_score > best_score.soft_score:
                        is_better = True

                    if is_better:
                        best_shift_days = trial_shift_days
                        best_violations = trial_violations
                        best_score = trial_score
                        improved = True
                        any_move = True
                        break

                if any_move:
                    break

        if not any_move:
            break

    final_status = SolveStatus.FEASIBLE if best_score.hard_score == 0 else SolveStatus.PARTIAL
    if best_score.hard_score == 0 and best_score.soft_score >= 0:
        final_status = SolveStatus.OPTIMAL

    return SolveResult(
        status=final_status,
        shift_days=best_shift_days,
        violations=best_violations,
        messages=[
            f"局所探索 (Ruin & Recreate) を実行しました (改善: {improved})",
            *initial_result.messages,
        ],
        stats={
            "local_search_improved": improved,
            "best_hard_score": best_score.hard_score,
            "best_soft_score": best_score.soft_score,
        },
        score=best_score,
    )
