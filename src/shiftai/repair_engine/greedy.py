"""Greedy assigner with multi-slot tracking for instant absence repair."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from shiftai.domain import PairingRule

from .candidates import ActionType, CandidateAction, CandidateEvaluator, _parse_slot_label
from .gap_scanner import SlotGap
from .snapshot import DaySnapshot

logger = logging.getLogger("shiftai.repair_engine.greedy")


def _slot_duration_hours(slot_label: str) -> float:
    parsed = _parse_slot_label(slot_label)
    return parsed.hours if parsed is not None else 0.5


class GreedyAssigner:
    """決定論的貪欲リペア割当器"""

    @classmethod
    def assign(
        cls,
        snapshot: DaySnapshot,
        gaps: Sequence[SlotGap],
        pairing_rules: Sequence[PairingRule] | None = None,
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

        # 現在の勤務状況・勤務時間を逐次トラッキング
        current_duty: dict[str, list[str]] = {
            sid: list(s_list) for sid, s_list in snapshot.on_duty.items()
        }
        current_hours: dict[str, float] = dict(snapshot.daily_worked_hours)

        for gap in sorted_gaps:
            rem_total = max(0, gap.gap_total)
            rem_qual = max(0, gap.gap_qualified)
            slot_hours = _slot_duration_hours(gap.slot_label)

            while rem_total > 0 or rem_qual > 0:
                step_gap = replace(
                    gap,
                    gap_total=rem_total,
                    gap_qualified=rem_qual,
                )
                chosen: CandidateAction | None = None

                # Phase 1: 当日出勤中職員の勤務時間延長（最優先）
                if prefer_on_duty_extension:
                    cands_ext: list[CandidateAction] = []
                    for sid in snapshot.on_duty:
                        if gap.slot_label in current_duty.get(sid, []):
                            continue
                        action = CandidateEvaluator.evaluate(
                            snapshot=snapshot,
                            gap=step_gap,
                            candidate_id=sid,
                            slots_to_assign=[gap.slot_label],
                            action_type=ActionType.EXTENSION,
                            pairing_rules=pairing_rules,
                            daily_hours_override=current_hours,
                            on_duty_override=current_duty,
                        )
                        if action:
                            cands_ext.append(action)
                    cands_ext.sort()
                    if cands_ext:
                        chosen = cands_ext[0]

                # Phase 2: 延長で埋まらない場合、公休職員の応援出勤（ヘルプ）を開放
                if chosen is None:
                    cands_help: list[CandidateAction] = []
                    for sid in snapshot.off_duty:
                        if gap.slot_label in current_duty.get(sid, []):
                            continue
                        action = CandidateEvaluator.evaluate(
                            snapshot=snapshot,
                            gap=step_gap,
                            candidate_id=sid,
                            slots_to_assign=[gap.slot_label],
                            action_type=ActionType.HELP_OFF_DUTY,
                            pairing_rules=pairing_rules,
                            daily_hours_override=current_hours,
                            on_duty_override=current_duty,
                        )
                        if action:
                            cands_help.append(action)
                    cands_help.sort()
                    if cands_help:
                        chosen = cands_help[0]

                if chosen is None:
                    break

                sid = chosen.staff_id
                assigned_slots.setdefault(sid, []).append(gap.slot_label)
                current_duty.setdefault(sid, []).append(gap.slot_label)
                current_hours[sid] = current_hours.get(sid, 0.0) + slot_hours

                prefix = (
                    "【延長】" if chosen.action_type == ActionType.EXTENSION else "【応援出勤】"
                )
                kind_desc = "延長配置" if chosen.action_type == ActionType.EXTENSION else "応援割当"
                action_logs.append(
                    f"{prefix}職員 {sid} をスロット {gap.slot_label} に{kind_desc} "
                    f"(スコア: {chosen.score:+.1f}, {', '.join(chosen.reasons)})"
                )

                rem_total = max(0, rem_total - 1)
                if snapshot.is_qualified(sid):
                    rem_qual = max(0, rem_qual - 1)

            if rem_total > 0 or rem_qual > 0:
                unfilled_gaps.append(
                    replace(
                        gap,
                        have_total=gap.need_total - rem_total,
                        have_qualified=gap.need_qualified - rem_qual,
                        gap_total=rem_total,
                        gap_qualified=rem_qual,
                    )
                )
                action_logs.append(
                    f"【未充足】スロット {gap.slot_label} の穴埋め候補が不足しています"
                    f"（残不足: 総数{rem_total}名 / 保育士{rem_qual}名）"
                )

        return assigned_slots, unfilled_gaps, action_logs
