"""Timefold 思想に基づくシフト評価スコアおよびスコア説明 (Score Explanation) エンジン。

ハード制約（法令・契約）の違反数とソフト制約（希望・公平性・運用）のペナルティを定量スコア化し、
制約ごとの内訳（Constraint Match）を生成・説明する。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from shiftai.domain import (
    CellState,
    ConstraintMatch,
    ObjectiveWeights,
    RequirementTable,
    ShiftDay,
    ShiftScore,
    StaffMember,
    StaffPreferences,
    Violation,
    ViolationSeverity,
)


def compute_shift_score(
    shift_days: Sequence[ShiftDay],
    violations: Sequence[Violation],
    requirements: RequirementTable | None = None,
    staff: Sequence[StaffMember] | None = None,
    preferences: Mapping[str, StaffPreferences] | None = None,
    weights: ObjectiveWeights | None = None,
) -> ShiftScore:
    """シフト配置結果と違反一覧から Timefold 互換の ShiftScore を計算する。"""
    w = weights or ObjectiveWeights()
    matches: dict[str, ConstraintMatch] = {}

    # 1. 違反一覧からハード/ソフトの個別制約マッチを集計
    violation_groups: dict[str, list[Violation]] = defaultdict(list)
    for v in violations:
        violation_groups[v.code].append(v)

    hard_penalty_count = 0
    soft_penalty_total = 0.0

    # 制約ごとのメタ情報（名前、重み、ハード判定）
    CONSTRAINT_METADATA: dict[str, tuple[str, bool, float]] = {
        "SHORTFALL_STAFF": ("必要人員配置基準", True, w.shortfall_penalty),
        "SHORTFALL_QUALIFIED": ("保育士比率配置基準", True, w.shortfall_penalty * 1.2),
        "BREAK_INSUFFICIENT": ("法定休憩時間確保", True, w.break_deficit_penalty),
        "SPLIT_SHIFT": ("1日勤務の3分割禁止", True, w.split_duty_penalty),
        "DAILY_HOURS_EXCEEDED": ("1日法定/契約労働時間上限", True, 2000.0),
        "CONSECUTIVE_DAYS": ("連続勤務日数上限", True, 1500.0),
        "REST_HOURS_SHORT": ("勤務間インターバル不足", True, 1500.0),
        "WORK_ON_UNAVAILABLE": ("希望休の尊重", False, w.preference_miss_penalty * 10),
        "OVERSTAFF": ("余剰配置抑制", False, w.overstaff_penalty),
        "BREAK_FRAGMENTED": ("休憩の分割防止", False, w.break_conflict_penalty),
        "BREAK_OVERLAP": ("同時休憩集中の防止", False, 10.0),
        "AVOID_EARLY_CONFLICT": ("早番回避希望の尊重", False, 20.0),
        "AVOID_LATE_CONFLICT": ("遅番回避希望の尊重", False, 20.0),
        "FAIRNESS_SPREAD": ("早番・遅番・土曜出勤の平準化", False, 10.0),
        "HOLIDAY_TARGET_DIFF": ("月間公休目標の平準化", False, 15.0),
    }

    for code, v_list in violation_groups.items():
        meta = CONSTRAINT_METADATA.get(code)
        if meta:
            c_name, is_hard, weight = meta
        else:
            is_hard = any(v.severity is ViolationSeverity.BLOCKER for v in v_list)
            c_name = v_list[0].message.split(" ")[0] if v_list else code
            weight = w.shortfall_penalty if is_hard else 5.0

        count = len(v_list)
        # スロット詳細情報が存在する場合、総スロット不足数を精密カウント
        if code in ("SHORTFALL_STAFF", "SHORTFALL_QUALIFIED") and requirements and shift_days:
            slot_shortfall = 0
            for sd in shift_days:
                d = sd.day
                for slot in requirements.slots:
                    if code == "SHORTFALL_STAFF":
                        needed = requirements.needed_staff(d, slot)
                        actual = sum(1 for a in sd.assignments.values() if a.get(slot.label) == CellState.WORK)
                        if actual < needed:
                            slot_shortfall += (needed - actual)
                    elif code == "SHORTFALL_QUALIFIED":
                        needed_q = requirements.needed_qualified(d, slot)
                        # 有資格者数のカウント
                        actual_q = 0
                        for sid, a in sd.assignments.items():
                            if a.get(slot.label) == CellState.WORK:
                                sm = next((s for s in (staff or ()) if s.staff_id == sid), None)
                                if sm and (sm.is_qualified or sm.has_role("保育士")):
                                    actual_q += 1
                        if actual_q < needed_q:
                            slot_shortfall += (needed_q - actual_q)
            if slot_shortfall > 0:
                count = slot_shortfall

        details = [f"{v.day} {v.slot.label if v.slot else ''}: {v.message}" for v in v_list]
        impact = count * weight

        if is_hard:
            hard_penalty_count += count
        else:
            soft_penalty_total += impact

        matches[code] = ConstraintMatch(
            constraint_id=code,
            constraint_name=c_name,
            is_hard=is_hard,
            weight=weight,
            match_count=count,
            score_impact=-impact,
            details=details,
        )

    # 2. 余剰配置 (Overstaff) のソフト評価集計 (RequirementTable とシフト配置がある場合)
    if requirements and shift_days:
        overstaff_slots = 0
        overstaff_details = []
        for sd in shift_days:
            d = sd.day
            for slot in requirements.slots:
                needed = requirements.needed_staff(d, slot)
                actual = sum(
                    1
                    for assigns in sd.assignments.values()
                    if assigns.get(slot.label) == CellState.WORK
                )
                if actual > needed and needed > 0:
                    diff = actual - needed
                    overstaff_slots += diff
                    if len(overstaff_details) < 10:
                        overstaff_details.append(
                            f"{d} {slot.label}: 必要 {needed} 名に対し {actual} 名配置 (+{diff}名)"
                        )

        if overstaff_slots > 0:
            c_code = "OVERSTAFF"
            meta = CONSTRAINT_METADATA.get(c_code, ("余剰配置抑制", False, w.overstaff_penalty))
            impact = overstaff_slots * meta[2]
            soft_penalty_total += impact
            matches[c_code] = ConstraintMatch(
                constraint_id=c_code,
                constraint_name=meta[0],
                is_hard=False,
                weight=meta[2],
                match_count=overstaff_slots,
                score_impact=-impact,
                details=overstaff_details,
            )

    hard_score = -hard_penalty_count
    soft_score = -soft_penalty_total

    return ShiftScore(
        hard_score=hard_score,
        soft_score=soft_score,
        matches=matches,
    )
