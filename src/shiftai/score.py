"""Timefold 思想に基づくシフト評価スコアおよびスコア説明 (Score Explanation) エンジン。

ハード制約（法令・契約）の違反数とソフト制約（希望・公平性・運用）のペナルティを定量スコア化し、
制約ごとの内訳（Constraint Match）を生成・説明する。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from shiftai.domain import (
    CellState,
    ConstraintMatch,
    Indictment,
    ObjectiveWeights,
    RequirementTable,
    Role,
    ShiftDay,
    ShiftScore,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    Violation,
    ViolationSeverity,
    qualified_count,
)


def compute_shift_score(
    shift_days: Sequence[ShiftDay],
    violations: Sequence[Violation],
    requirements: RequirementTable | None = None,
    staff: Sequence[StaffMember] | None = None,
    preferences: Mapping[str, StaffPreferences] | None = None,
    weights: ObjectiveWeights | None = None,
    standard: StaffingStandard | None = None,
) -> ShiftScore:
    """シフト配置結果と違反一覧から Timefold 互換の ShiftScore を計算する。

    ``standard`` が与えられると SHORTFALL_QUALIFIED の有資格者数を
    ``domain.qualified_count`` に委譲し（nurse_as_qualified_cap /
    qualified_extra_roles を反映）、None なら従来の保育士のみカウント
    動作を維持する（後方互換）。
    """
    w = weights or ObjectiveWeights()
    matches: dict[str, ConstraintMatch] = {}

    # 1. 違反一覧からハード/ソフトの個別制約マッチを集計
    violation_groups: dict[str, list[Violation]] = defaultdict(list)
    for v in violations:
        violation_groups[v.code].append(v)

    hard_penalty_count = 0
    medium_penalty_total = 0.0
    soft_penalty_total = 0.0

    # 特定エンティティ（職員×日付）ごとのペナルティ起因分析データ
    indictment_accum: dict[tuple[str, Any], dict[str, Any]] = defaultdict(
        lambda: {"hard": 0, "soft": 0.0, "violations": []}
    )

    # 制約ごとのメタ情報（名前、スコア階層: HARD/MEDIUM/SOFT、重み）
    CONSTRAINT_METADATA: dict[str, tuple[str, str, float]] = {
        # 1. HARD (法令・基準・労基法・配置基準: 絶対遵守)
        "SHORTFALL_STAFF": ("必要人員配置基準", "HARD", w.shortfall_penalty),
        "SHORTFALL_QUALIFIED": ("保育士比率配置基準", "HARD", w.shortfall_penalty * 1.2),
        "BREAK_INSUFFICIENT": ("法定休憩時間確保", "HARD", w.break_deficit_penalty),
        "SPLIT_SHIFT": ("1日勤務の3分割禁止", "HARD", w.split_duty_penalty),
        "DAILY_HOURS_EXCEEDED": ("1日法定/契約労働時間上限", "HARD", 2000.0),
        "CONSECUTIVE_DAYS": ("連続勤務日数上限", "HARD", 1500.0),
        "REST_HOURS_SHORT": ("勤務間インターバル不足", "HARD", 1500.0),
        "MAIN_OPENING_COVERAGE": ("11時間開所常時2名基準", "HARD", 3000.0),
        # 2. MEDIUM (財務・事業運営・余剰・契約枠)
        "OVERSTAFF": ("余剰配置抑制", "MEDIUM", w.overstaff_penalty),
        "HOLIDAY_TARGET_DIFF": ("月間公休目標の乖離", "MEDIUM", 15.0),
        "WEEKLY_HOURS_EXCEEDED": ("週40時間超過", "MEDIUM", 50.0),
        "OVERCONSTRAINED_SHORTFALL": ("外部応援・欠員枠", "MEDIUM", 10000.0),
        # 3. SOFT (従業員満足度・希望・公平性・QoL)
        "WORK_ON_UNAVAILABLE": ("希望休の尊重", "SOFT", w.preference_miss_penalty * 10),
        "BREAK_FRAGMENTED": ("休憩の分割防止", "SOFT", w.break_conflict_penalty),
        "BREAK_OVERLAP": ("同時休憩集中の防止", "SOFT", 10.0),
        "AVOID_EARLY_CONFLICT": ("早番回避希望の尊重", "SOFT", 20.0),
        "AVOID_LATE_CONFLICT": ("遅番回避希望の尊重", "SOFT", 20.0),
        "FAIRNESS_SPREAD": ("早番・遅番・土曜出勤の平準化", "SOFT", 10.0),
    }

    for code, v_list in violation_groups.items():
        meta = CONSTRAINT_METADATA.get(code)
        if meta:
            c_name, level_str, weight = meta
            is_hard = level_str == "HARD"
        else:
            is_hard = any(v.severity is ViolationSeverity.BLOCKER for v in v_list)
            level_str = "HARD" if is_hard else "SOFT"
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
                        actual = sum(
                            1
                            for a in sd.assignments.values()
                            if a.get(slot.label) == CellState.WORK
                        )
                        if actual < needed:
                            slot_shortfall += needed - actual
                    elif code == "SHORTFALL_QUALIFIED":
                        needed_q = requirements.needed_qualified(d, slot)
                        # 有資格者数のカウント
                        working_members = [
                            sm
                            for sid, a in sd.assignments.items()
                            if a.get(slot.label) == CellState.WORK
                            for sm in (staff or ())
                            if sm.staff_id == sid
                        ]
                        if standard is not None:
                            # standard（nurse_as_qualified_cap / qualified_extra_roles 等）を反映
                            actual_q = qualified_count(working_members, standard)
                        else:
                            # 後方互換: 保育士のみを数える
                            actual_q = 0
                            for sm in working_members:
                                is_qual = (
                                    getattr(sm, "is_qualified", False)
                                    or sm.has_role(Role.HOIKUSHI)
                                    or sm.has_role("保育士")
                                    or any(
                                        getattr(r, "value", str(r)) == "保育士" for r in sm.roles
                                    )
                                )
                                if is_qual:
                                    actual_q += 1
                        if actual_q < needed_q:
                            slot_shortfall += needed_q - actual_q
            if slot_shortfall > 0:
                count = slot_shortfall

        details = [f"{v.day} {v.slot.label if v.slot else ''}: {v.message}" for v in v_list]
        impact = count * weight

        # 各違反についてエンティティ（職員×日付）へ起因（Indictment）を振り分け
        for v in v_list:
            ent_staff = v.staff_id if v.staff_id else "__FACILITY__"
            ent_key = (ent_staff, v.day)
            if is_hard:
                indictment_accum[ent_key]["hard"] += 1
            else:
                indictment_accum[ent_key]["soft"] += weight
            v_desc = f"[{c_name}] {v.message}"
            if v_desc not in indictment_accum[ent_key]["violations"]:
                indictment_accum[ent_key]["violations"].append(v_desc)

        if is_hard:
            hard_penalty_count += count
        elif level_str == "MEDIUM":
            medium_penalty_total += impact
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

    # 2. 余剰配置 (Overstaff) のミディアム評価集計 (RequirementTable とシフト配置がある場合)
    if requirements and shift_days and "OVERSTAFF" not in matches:
        # violations 由来で matches に OVERSTAFF が既にある場合は再集計をスキップし
        # 二重計上を防ぐ（後方互換のため standard なしでも動作する）。
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
            meta = CONSTRAINT_METADATA.get(c_code, ("余剰配置抑制", "MEDIUM", w.overstaff_penalty))
            impact = overstaff_slots * meta[2]
            medium_penalty_total += impact
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
    medium_score = -medium_penalty_total
    soft_score = -soft_penalty_total

    # Indictment オブジェクトの構築
    indictments: dict[tuple[str, Any], Indictment] = {}
    for (sid, v_day), data in indictment_accum.items():
        indictments[(sid, v_day)] = Indictment(
            staff_id=sid,
            day=v_day,
            hard_penalty_count=data["hard"],
            soft_penalty_total=data["soft"],
            violations=data["violations"],
        )

    return ShiftScore(
        hard_score=hard_score,
        medium_score=medium_score,
        soft_score=soft_score,
        matches=matches,
        indictments=indictments,
    )
