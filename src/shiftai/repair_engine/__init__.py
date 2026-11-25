"""NuShift Fast Absence Repair Engine (Tier 0 Rule Engine with Fallbacks).

朝の突発欠勤に対して数理最適化（MILP）をバイパスし、
決定論的ルールエンジンによりマイクロ秒〜ミリ秒（<1ms）で高速に穴埋め代行案を返却する。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from shiftai.domain import (
    ChildPlan,
    ClassRoom,
    FacilitySettings,
    PairingRule,
    RequirementTable,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
)
from shiftai.monthly import ObjectiveWeights, SolveResult
from shiftai.shift_patterns import ShiftPattern

from .candidates import ActionType, CandidateAction, CandidateEvaluator
from .gap_scanner import SlotGap, SlotGapScanner
from .greedy import GreedyAssigner
from .outcome import RepairOutcome
from .snapshot import DaySnapshot, SnapshotIndex

__all__ = [
    "ActionType",
    "CandidateAction",
    "CandidateEvaluator",
    "DaySnapshot",
    "GreedyAssigner",
    "RepairOutcome",
    "SlotGap",
    "SlotGapScanner",
    "SnapshotIndex",
    "repair_shift_absence_fast",
]

logger = logging.getLogger("shiftai.repair_engine")


def repair_shift_absence_fast(
    existing_result: SolveResult,
    absent_staff_id: str | Sequence[str],
    target_date: date,
    children: Sequence[ChildPlan],
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    weights: ObjectiveWeights | None = None,
    settings: FacilitySettings | None = None,
    standard: StaffingStandard | None = None,
    patterns: Sequence[ShiftPattern] | None = None,
    pairing_rules: Sequence[PairingRule] | None = None,
    classrooms: Sequence[ClassRoom] | None = None,
    require_leader: bool = False,
    require_opening_closing_skill: bool = False,
    time_limit_sec: int = 10,
    prefer_on_duty_extension: bool = True,
    fallback_milp_fn: Any | None = None,
) -> SolveResult:
    """突発欠勤に対する超高速ルールベースリペア（Tier 0）およびフォールバック制御。

    環境変数 `NU_SHIFT_REPAIR_MODE`:
        - `auto` (既定): Tier 0 (ルール) ➔ 未充足時のみ Tier 2 (MILP) へフォールバック
        - `rule`: Tier 0 のみ（完全決定論・<1ms）
        - `milp`: 従来の MILP パスのみ
    """
    mode = os.getenv("NU_SHIFT_REPAIR_MODE", "auto").strip().lower()
    start_t = time.perf_counter()

    absent_ids = [absent_staff_id] if isinstance(absent_staff_id, str) else list(absent_staff_id)

    # 1. モードが milp 指定の場合は直接フォールバック
    if mode == "milp":
        if fallback_milp_fn:
            return fallback_milp_fn()
        raise RuntimeError("MILP fallback function is not provided.")

    # 2. Tier 0: ルールエンジンの実行
    outcome: RepairOutcome | None = None
    try:
        contracts = {str(s.staff_id): s.contract for s in staff}
        # A. 当日スナップショットの O(1) 構築
        snapshot = SnapshotIndex.build(
            existing_assignments=existing_result.assignments,
            target_date=target_date,
            staff=staff,
            absent_staff_ids=absent_ids,
            contracts=contracts,
            preferences=preferences,
            granularity_min=getattr(settings, "granularity_min", 30) if settings else 30,
        )

        # B. 不足スロット (Gap) の高速走査
        gaps = SlotGapScanner.scan(
            snapshot=snapshot,
            requirements=requirements,
            require_leader=require_leader,
        )

        # 不足が元々ゼロであれば欠勤者をOFFにしただけで即返却
        if not gaps:
            elapsed = round(time.perf_counter() - start_t, 4)
            outcome = RepairOutcome(
                status="SUCCESS",
                assigned_slots={},
                unfilled_gaps=[],
                action_logs=["欠勤による配置不足はありません（必要人員充足維持）"],
                tier=0,
                elapsed_sec=elapsed,
            )
            return outcome.to_solve_result(existing_result, target_date, absent_ids)

        # C. 貪欲割当（延長優先 ＋ 複数不足対応）
        assigned_slots, unfilled_gaps, action_logs = GreedyAssigner.assign(
            snapshot=snapshot,
            gaps=gaps,
            pairing_rules=pairing_rules,
            prefer_on_duty_extension=prefer_on_duty_extension,
        )

        elapsed = round(time.perf_counter() - start_t, 4)
        outcome = RepairOutcome(
            status="SUCCESS" if not unfilled_gaps else "PARTIAL",
            assigned_slots=assigned_slots,
            unfilled_gaps=unfilled_gaps,
            action_logs=action_logs,
            tier=0,
            elapsed_sec=elapsed,
        )

        # 完全充足できた場合、または mode=rule の場合は即返却
        if outcome.is_fully_repaired or mode == "rule" or not fallback_milp_fn:
            logger.info(
                "Tier 0 ルールリペア完了: 欠勤=%s, 追加割当=%d件, 未充足=%d件, 所要時間=%.2fms",
                absent_ids,
                sum(len(v) for v in assigned_slots.values()),
                len(unfilled_gaps),
                elapsed * 1000,
            )
            return outcome.to_solve_result(existing_result, target_date, absent_ids)

    except Exception as exc:
        logger.exception("Tier 0 ルールリペア中に例外発生: %s", exc)
        if mode == "rule" or not fallback_milp_fn:
            raise

    # 3. 未充足があり、かつ mode == "auto" の場合は Tier 2 (MILP) へフォールバック
    logger.warning("Tier 0 で未充足が発生したため、Tier 2 (MILP) へフォールバックします。")
    if fallback_milp_fn:
        return fallback_milp_fn()

    assert outcome is not None
    return outcome.to_solve_result(existing_result, target_date, absent_ids)
