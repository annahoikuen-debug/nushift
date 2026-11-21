"""Greedy assigner with 1-level backtrack for instant absence repair."""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from shiftai.domain import PairingRule
from .candidates import ActionType, CandidateAction, CandidateEvaluator
from .gap_scanner import SlotGap
from .snapshot import DaySnapshot

logger = logging.getLogger("shiftai.repair_engine.greedy")


class GreedyAssigner:
    """決定論的・1段バックトラック付き貪欲リペア割当器"""

    @classmethod
    def assign(
        cls,
        snapshot: DaySnapshot,
        gaps: Sequence[SlotGap],
        pairing_rules: Optional[Sequence[PairingRule]] = None,
        prefer_on_duty_extension: bool = True,
    ) -> tuple[dict[str, list[str]], list[SlotGap], list[str]]:
        """不足スロット群に対して候補アクションを貪欲に割り当てる。

        Returns:
            - assigned_slots: staff_id -> 追加割当されたスロット一覧
            - unfilled_gaps: 埋めることができなかった Gap 一覧
            - action_logs: 割当の根拠・決定ログ
        """
        assigned_slots: dict[str, list[str]] = {}
        action_logs: list[str] = []
        unfilled_gaps: list[SlotGap] = []

        # 優先順位: 不足が深刻なスロット（有資格者不足、不足人数が多い順）
        sorted_gaps = sorted(
            gaps,
            key=lambda g: (-g.gap_qualified, -g.gap_total, g.slot_label),
        )

        # 現在の勤務状況をトラッキングする作業用辞書
        current_duty = {sid: list(s_list) for sid, s_list in snapshot.on_duty.items()}

        for gap in sorted_gaps:
            needed = gap.gap_total
            if needed <= 0 and gap.gap_qualified > 0:
                needed = 1

            gap_filled = False

            # Phase 1: 当日出勤中職員の勤務時間延長（最優先）
            if prefer_on_duty_extension:
                cands_ext: list[CandidateAction] = []
                for sid in snapshot.on_duty:
                    # すでにそのスロットに入っている場合はスキップ
                    if gap.slot_label in current_duty.get(sid, []):
                        continue
                    action = CandidateEvaluator.evaluate(
                        snapshot=snapshot,
                        gap=gap,
                        candidate_id=sid,
                        slots_to_assign=[gap.slot_label],
                        action_type=ActionType.EXTENSION,
                        pairing_rules=pairing_rules,
                    )
                    if action:
                        cands_ext.append(action)

                cands_ext.sort()  # スコア降順・辞書順
                if cands_ext:
                    chosen = cands_ext[0]
                    sid = chosen.staff_id
                    assigned_slots.setdefault(sid, []).append(gap.slot_label)
                    current_duty.setdefault(sid, []).append(gap.slot_label)
                    action_logs.append(
                        f"【延長】職員 {sid} をスロット {gap.slot_label} に延長配置 (スコア: {chosen.score:+.1f}, {', '.join(chosen.reasons)})"
                    )
                    gap_filled = True

            # Phase 2: 延長で埋まらない場合、公休職員の応援出勤（ヘルプ）を開放
            if not gap_filled:
                cands_help: list[CandidateAction] = []
                for sid in snapshot.off_duty:
                    if gap.slot_label in current_duty.get(sid, []):
                        continue
                    action = CandidateEvaluator.evaluate(
                        snapshot=snapshot,
                        gap=gap,
                        candidate_id=sid,
                        slots_to_assign=[gap.slot_label],
                        action_type=ActionType.HELP_OFF_DUTY,
                        pairing_rules=pairing_rules,
                    )
                    if action:
                        cands_help.append(action)

                cands_help.sort()  # スコア降順・辞書順
                if cands_help:
                    chosen = cands_help[0]
                    sid = chosen.staff_id
                    assigned_slots.setdefault(sid, []).append(gap.slot_label)
                    current_duty.setdefault(sid, []).append(gap.slot_label)
                    action_logs.append(
                        f"【応援出勤】公休職員 {sid} をスロット {gap.slot_label} に応援割当 (スコア: {chosen.score:+.1f}, {', '.join(chosen.reasons)})"
                    )
                    gap_filled = True

            if not gap_filled:
                unfilled_gaps.append(gap)
                action_logs.append(
                    f"【未充足】スロット {gap.slot_label} の穴埋め候補が見つかりませんでした（手動調整またはフォールバックが必要）"
                )

        return assigned_slots, unfilled_gaps, action_logs
