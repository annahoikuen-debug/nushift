"""Timefold 思想に基づく高度な近傍探索（Local Search）および Ruin & Recreate エンジン。

多様な近傍ムーブ（SwapMove / ChangeMove / AddShiftMove / DaySwapMove）と、
Timefold 標準のメタヒューリスティクスである Late Acceptance Hill Climbing (LAHC)
により、MILP のタイムアウト解や暫定解の制約違反を高速かつ大域的に解消する。
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
    ShiftScore,
    Slot,
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


def _apply_pattern_to_day(
    target_assignments: dict[str, dict[str, CellState]],
    staff_member: StaffMember,
    pattern: ShiftPattern,
    slots: Sequence[Slot],
    settings: FacilitySettings | None = None,
) -> None:
    """指定された職員の当日のスロット割り当てにパターン（勤務・休憩）を適用する。"""
    p_start = to_minutes(pattern.start)
    p_end = to_minutes(pattern.end)
    span_m = p_end - p_start

    b_target = (
        staff_member.contract.break_minutes
        if staff_member.contract.break_minutes is not None
        else (settings.break_minutes if settings is not None else 60)
    )
    has_break = span_m > 6 * 60
    if has_break:
        actual_break_m = max(b_target, 60) if span_m > 8 * 60 else max(b_target, 45)
        granularity = slots[0].minutes if slots else 30
        break_slots_count = max(1, round(actual_break_m / granularity))
        break_duration = break_slots_count * granularity
        mid_m = p_start + (span_m - break_duration) // 2
        break_start = (mid_m // granularity) * granularity
        break_end = break_start + break_duration
    else:
        break_start = 0
        break_end = 0

    s_map: dict[str, CellState] = {}
    for slot in slots:
        if p_start <= slot.start_minutes and slot.end_minutes <= p_end:
            if has_break and (break_start <= slot.start_minutes < break_end):
                s_map[slot.label] = CellState.BREAK
            else:
                s_map[slot.label] = CellState.WORK
        else:
            s_map[slot.label] = CellState.OFF

    target_assignments[staff_member.staff_id] = s_map


def _is_staff_fixed_on_day(
    staff_id: str,
    target_day: date,
    slots: Sequence[Slot],
    fixed: Mapping[tuple[str, date, str], CellState],
) -> bool:
    """職員が対象日に固定セル（手動ピン留め）を持っているか。"""
    return any((staff_id, target_day, sl.label) in fixed for sl in slots)


def _clone_shift_days(
    shift_days: Sequence[ShiftDay],
    modified_days: set[date],
) -> list[ShiftDay]:
    """変更日のみディープコピーし、他の日は再利用して高速化する差分クローン。"""
    res: list[ShiftDay] = []
    for sd in shift_days:
        if sd.day in modified_days:
            res.append(
                ShiftDay(
                    day=sd.day,
                    assignments={sid: dict(smap) for sid, smap in sd.assignments.items()},
                )
            )
        else:
            res.append(sd)
    return res


def ruin_and_recreate(
    initial_result: SolveResult,
    requirements: RequirementTable,
    staff: Sequence[StaffMember],
    preferences: Mapping[str, StaffPreferences] | None = None,
    standard: StaffingStandard | None = None,
    settings: FacilitySettings | None = None,
    fixed_assignments: Mapping[tuple[str, date, str], CellState] | None = None,
    patterns: Sequence[ShiftPattern] | None = None,
    weights: Any | None = None,
    max_iterations: int = 50,
    lahc_size: int = 20,
) -> SolveResult:
    """Timefold の LAHC メタヒューリスティクスおよび多角ムーブによる局所探索フォールバック。"""
    prefs = preferences or {}
    fixed = fixed_assignments or {}
    cand_patterns = list(patterns or default_patterns())
    slots = tuple(requirements.slots)
    placeable_staff = [s for s in staff if s.is_placeable]

    current_shift_days = [copy.deepcopy(sd) for sd in initial_result.shift_days]
    existing_days = {sd.day for sd in current_shift_days}

    # 初期解に欠けている日があれば全職員 OFF (fixed は反映) で補完
    req_days = list(requirements.rows.keys()) if hasattr(requirements, "rows") else []
    for d in req_days:
        if d not in existing_days:
            day_assign: dict[str, dict[str, CellState]] = {}
            for s in staff:
                s_map: dict[str, CellState] = {}
                for sl in slots:
                    fixed_val = fixed.get((s.staff_id, d, sl.label))
                    s_map[sl.label] = fixed_val if fixed_val is not None else CellState.OFF
                day_assign[s.staff_id] = s_map
            current_shift_days.append(ShiftDay(day=d, assignments=day_assign))

    # 初期スコアを算出
    current_violations = check_violations(
        requirements,
        SolveResult(status=initial_result.status, shift_days=current_shift_days, violations=[]),
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
        weights=weights,
    )

    best_shift_days = [copy.deepcopy(sd) for sd in current_shift_days]
    best_violations = list(current_violations)
    best_score = current_score

    # Timefold の Late Acceptance Hill Climbing (LAHC) リスト初期化
    lahc_history: list[ShiftScore] = [current_score] * max(1, lahc_size)
    improved = False
    step = 0

    for _iteration in range(max_iterations):
        if best_score.hard_score == 0 and len(best_violations) == 0:
            break

        # 違反日の特定
        violated_dates = {v.day for v in best_violations if v.day}
        if not violated_dates:
            violated_dates = {sd.day for sd in best_shift_days}

        any_move_accepted = False

        for target_day in sorted(violated_dates):
            current_sd = next((d for d in current_shift_days if d.day == target_day), None)
            if not current_sd:
                continue

            # 出勤職員と非勤務職員を分類
            working_staff: list[StaffMember] = []
            off_staff: list[StaffMember] = []
            for s in placeable_staff:
                if any(
                    current_sd.assignments.get(s.staff_id, {}).get(sl.label) == CellState.WORK
                    for sl in slots
                ):
                    working_staff.append(s)
                else:
                    off_staff.append(s)

            # -------------------------------------------------------------
            # ムーブ 1: SwapMove（同一日の出勤者2名のパターンスワップ）
            # -------------------------------------------------------------
            if len(working_staff) >= 2:
                for i in range(len(working_staff)):
                    s1 = working_staff[i]
                    if _is_staff_fixed_on_day(s1.staff_id, target_day, slots, fixed):
                        continue
                    for j in range(i + 1, len(working_staff)):
                        s2 = working_staff[j]
                        if _is_staff_fixed_on_day(s2.staff_id, target_day, slots, fixed):
                            continue

                        # 2名の割り当てを交換
                        trial_days = _clone_shift_days(current_shift_days, {target_day})
                        target_trial_sd = next(d for d in trial_days if d.day == target_day)
                        a1 = dict(target_trial_sd.assignments.get(s1.staff_id, {}))
                        a2 = dict(target_trial_sd.assignments.get(s2.staff_id, {}))
                        target_trial_sd.assignments[s1.staff_id] = a2
                        target_trial_sd.assignments[s2.staff_id] = a1

                        # スコア評価
                        trial_res = SolveResult(
                            status=SolveStatus.FEASIBLE,
                            shift_days=trial_days,
                            violations=[],
                        )
                        trial_viols = check_violations(
                            requirements,
                            trial_res,
                            staff,
                            prefs,
                            standard=standard,
                            settings=settings,
                        )
                        trial_score = compute_shift_score(
                            shift_days=trial_days,
                            violations=trial_viols,
                            requirements=requirements,
                            staff=staff,
                            preferences=prefs,
                            weights=weights,
                        )

                        # LAHC 受容判定
                        accepted = trial_score >= lahc_history[step % len(lahc_history)]
                        if accepted:
                            current_shift_days = trial_days
                            current_violations = trial_viols
                            current_score = trial_score
                            any_move_accepted = True

                        if trial_score > best_score:
                            best_shift_days = [copy.deepcopy(sd) for sd in trial_days]
                            best_violations = list(trial_viols)
                            best_score = trial_score
                            improved = True

                        lahc_history[step % len(lahc_history)] = current_score
                        step += 1
                        if any_move_accepted:
                            break
                    if any_move_accepted:
                        break

            if any_move_accepted:
                continue

            # -------------------------------------------------------------
            # ムーブ 2: ChangeMove（出勤者の勤務パターン変更）
            # -------------------------------------------------------------
            for sm in working_staff:
                if _is_staff_fixed_on_day(sm.staff_id, target_day, slots, fixed):
                    continue
                pref = prefs.get(sm.staff_id)
                for pat in cand_patterns:
                    p_start = to_minutes(pat.start)
                    p_end = to_minutes(pat.end)
                    if pref and any(
                        pref.is_unavailable(target_day, sl)
                        for sl in slots
                        if p_start <= sl.start_minutes and sl.end_minutes <= p_end
                    ):
                        continue

                    trial_days = _clone_shift_days(current_shift_days, {target_day})
                    target_trial_sd = next(d for d in trial_days if d.day == target_day)
                    _apply_pattern_to_day(target_trial_sd.assignments, sm, pat, slots, settings)

                    trial_res = SolveResult(
                        status=SolveStatus.FEASIBLE,
                        shift_days=trial_days,
                        violations=[],
                    )
                    trial_viols = check_violations(
                        requirements,
                        trial_res,
                        staff,
                        prefs,
                        standard=standard,
                        settings=settings,
                    )
                    trial_score = compute_shift_score(
                        shift_days=trial_days,
                        violations=trial_viols,
                        requirements=requirements,
                        staff=staff,
                        preferences=prefs,
                        weights=weights,
                    )

                    accepted = trial_score >= lahc_history[step % len(lahc_history)]
                    if accepted:
                        current_shift_days = trial_days
                        current_violations = trial_viols
                        current_score = trial_score
                        any_move_accepted = True

                    if trial_score > best_score:
                        best_shift_days = [copy.deepcopy(sd) for sd in trial_days]
                        best_violations = list(trial_viols)
                        best_score = trial_score
                        improved = True

                    lahc_history[step % len(lahc_history)] = current_score
                    step += 1
                    if any_move_accepted:
                        break
                if any_move_accepted:
                    break

            if any_move_accepted:
                continue

            # -------------------------------------------------------------
            # ムーブ 3: AddShiftMove（非勤務者への勤務パターン割当）
            # -------------------------------------------------------------
            for sm in off_staff:
                if _is_staff_fixed_on_day(sm.staff_id, target_day, slots, fixed):
                    continue
                pref = prefs.get(sm.staff_id)
                for pat in cand_patterns:
                    p_start = to_minutes(pat.start)
                    p_end = to_minutes(pat.end)
                    if pref and any(
                        pref.is_unavailable(target_day, sl)
                        for sl in slots
                        if p_start <= sl.start_minutes and sl.end_minutes <= p_end
                    ):
                        continue

                    trial_days = _clone_shift_days(current_shift_days, {target_day})
                    target_trial_sd = next(d for d in trial_days if d.day == target_day)
                    _apply_pattern_to_day(target_trial_sd.assignments, sm, pat, slots, settings)

                    trial_res = SolveResult(
                        status=SolveStatus.FEASIBLE,
                        shift_days=trial_days,
                        violations=[],
                    )
                    trial_viols = check_violations(
                        requirements,
                        trial_res,
                        staff,
                        prefs,
                        standard=standard,
                        settings=settings,
                    )
                    trial_score = compute_shift_score(
                        shift_days=trial_days,
                        violations=trial_viols,
                        requirements=requirements,
                        staff=staff,
                        preferences=prefs,
                        weights=weights,
                    )

                    accepted = trial_score >= lahc_history[step % len(lahc_history)]
                    if accepted:
                        current_shift_days = trial_days
                        current_violations = trial_viols
                        current_score = trial_score
                        any_move_accepted = True

                    if trial_score > best_score:
                        best_shift_days = [copy.deepcopy(sd) for sd in trial_days]
                        best_violations = list(trial_viols)
                        best_score = trial_score
                        improved = True

                    lahc_history[step % len(lahc_history)] = current_score
                    step += 1
                    if any_move_accepted:
                        break
                if any_move_accepted:
                    break

        if not any_move_accepted:
            break

    final_status = SolveStatus.FEASIBLE if best_score.hard_score == 0 else SolveStatus.PARTIAL
    if best_score.hard_score == 0 and best_score.soft_score >= 0 and best_score.medium_score >= 0:
        final_status = SolveStatus.OPTIMAL

    return SolveResult(
        status=final_status,
        shift_days=best_shift_days,
        violations=best_violations,
        messages=[
            f"高度近傍探索 (Timefold LAHC & Swap/Change Moves) を実行しました (改善: {improved}, 探索ステップ: {step})",
            *initial_result.messages,
        ],
        stats={
            "local_search_improved": improved,
            "local_search_steps": step,
            "best_hard_score": best_score.hard_score,
            "best_medium_score": best_score.medium_score,
            "best_soft_score": best_score.soft_score,
        },
        score=best_score,
    )
