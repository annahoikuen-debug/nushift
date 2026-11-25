"""Candidate evaluation: Hard filters and soft scoring for repair actions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import time as dt_time
from enum import Enum

from shiftai.domain import PairingRule, Slot, daily_cap_minutes

from .gap_scanner import SlotGap
from .snapshot import DaySnapshot


class ActionType(str, Enum):
    """リペア行動の種類"""

    EXTENSION = "EXTENSION"  # 当日出勤中職員の勤務時間延長
    HELP_OFF_DUTY = "HELP_OFF_DUTY"  # 当日公休職員の応援出勤


@dataclass(frozen=True)
class CandidateAction:
    """評価済み候補行動"""

    staff_id: str
    action_type: ActionType
    slots_to_add: tuple[str, ...]
    score: float
    reasons: tuple[str, ...]

    def __lt__(self, other: CandidateAction) -> bool:
        # スコア降順、同点は staff_id 昇順（決定論）
        if self.score != other.score:
            return self.score > other.score
        return self.staff_id < other.staff_id


def _parse_slot_label(label: str) -> Slot | None:
    """'HH:MM-HH:MM' 形式のスロットラベルから Slot を復元する。"""
    if "-" not in label:
        return None
    try:
        start_s, end_s = label.split("-", 1)
        sh, sm = (int(x) for x in start_s.strip().split(":", 1))
        eh, em = (int(x) for x in end_s.strip().split(":", 1))
        end_t = dt_time(0, 0) if eh == 24 and em == 0 else dt_time(eh, em)
        return Slot(start=dt_time(sh, sm), end=end_t)
    except (ValueError, TypeError):
        return None


class CandidateEvaluator:
    """候補職員の適格性判定（ハードフィルタ）および優先度スコア計算器"""

    @staticmethod
    def evaluate(
        snapshot: DaySnapshot,
        gap: SlotGap,
        candidate_id: str,
        slots_to_assign: Sequence[str],
        action_type: ActionType,
        pairing_rules: Sequence[PairingRule] | None = None,
        max_daily_hours: float = 10.0,
        daily_hours_override: Mapping[str, float] | None = None,
        on_duty_override: Mapping[str, Sequence[str]] | None = None,
    ) -> CandidateAction | None:
        """候補職員が gap を埋められるかを検証し、適格ならスコア付き CandidateAction を返す。
        不適格なら None を返す（ハードフィルタ）。
        """
        # H1: 欠勤者本人は除外
        if candidate_id in snapshot.absent:
            return None

        # H2: 保育士資格が必要なスロットで無資格者は除外
        if gap.gap_qualified > 0 and not snapshot.is_qualified(candidate_id):
            return None

        # H3: リーダー必須スロットで非リーダーは除外
        if gap.require_leader and not snapshot.is_leader(candidate_id):
            return None

        parsed_slots = [
            sl for label in slots_to_assign if (sl := _parse_slot_label(label)) is not None
        ]

        # H4: 希望休・出勤不可時間帯（Unavailability）との重複を除外
        pref = snapshot.preferences.get(candidate_id)
        if pref is not None:
            for sl in parsed_slots:
                if pref.is_unavailable(snapshot.day, sl):
                    return None

        # H6: 契約の勤務可能時間帯（earliest_start 〜 latest_end）チェック
        contract = snapshot.contracts.get(candidate_id)
        if contract is not None:
            earliest_min = contract.earliest_start.hour * 60 + contract.earliest_start.minute
            latest_min = contract.latest_end.hour * 60 + contract.latest_end.minute
            if latest_min == 0:
                latest_min = 24 * 60
            for sl in parsed_slots:
                if sl.start_minutes < earliest_min or sl.end_minutes > latest_min:
                    return None

        # H7: 1日上限（労基法・36協定・契約上限）チェック
        hours_source = (
            daily_hours_override
            if daily_hours_override is not None
            else snapshot.daily_worked_hours
        )
        current_hours = hours_source.get(candidate_id, 0.0)
        if parsed_slots and len(parsed_slots) == len(slots_to_assign):
            additional_hours = sum(sl.hours for sl in parsed_slots)
        else:
            additional_hours = len(slots_to_assign) * 0.5
        new_total_hours = current_hours + additional_hours

        effective_cap = max_daily_hours
        if contract is not None:
            statutory_cap = daily_cap_minutes(contract) / 60.0
            contract_max = contract.max_daily_hours or statutory_cap
            effective_cap = min(max_daily_hours, statutory_cap, contract_max)

        if new_total_hours > effective_cap + 1e-6:
            return None

        # H8: 連勤上限チェック
        if contract and getattr(contract, "max_consecutive_days", 0) > 0:
            if snapshot.consecutive_workdays.get(candidate_id, 0) >= contract.max_consecutive_days:
                if action_type == ActionType.HELP_OFF_DUTY:
                    return None

        # H5: ペアリング制約（avoid_together 相手と同一スロット同時勤務にならないか）
        if pairing_rules:
            duty_map = on_duty_override if on_duty_override is not None else snapshot.on_duty
            target_labels = set(slots_to_assign)
            same_slot_staff = {
                sid
                for sid, s_list in duty_map.items()
                if sid != candidate_id and (set(s_list) & target_labels)
            }
            for pr in pairing_rules:
                is_avoid = getattr(pr, "kind", "") == "avoid_together"
                if is_avoid:
                    a_id = getattr(pr, "staff_a_id", None)
                    b_id = getattr(pr, "staff_b_id", None)
                    if candidate_id == a_id and b_id in same_slot_staff:
                        return None
                    if candidate_id == b_id and a_id in same_slot_staff:
                        return None

        # --- ソフトスコア計算 ---
        score = 0.0
        reasons: list[str] = []

        if action_type == ActionType.EXTENSION:
            score += 30.0
            reasons.append("当日出勤者による勤務時間延長 (+30)")
        else:
            score -= 50.0
            reasons.append("公休からの応援出勤 (-50)")

        staff_class = snapshot.assigned_classes.get(candidate_id)
        if gap.class_id and staff_class:
            if staff_class == gap.class_id:
                score += 40.0
                reasons.append(f"同クラス({gap.class_id})担当継続 (+40)")
            else:
                score -= 15.0
                reasons.append("他クラスへの応援 (-15)")

        if current_hours <= 4.0:
            score += 15.0
            reasons.append("当日の勤務時間に余裕あり (+15)")
        elif current_hours >= 8.0:
            score -= 10.0
            reasons.append("実働8h超過残業 (-10)")

        if snapshot.is_qualified(candidate_id):
            score += 10.0
            reasons.append("保育士資格保有 (+10)")

        return CandidateAction(
            staff_id=candidate_id,
            action_type=action_type,
            slots_to_add=tuple(slots_to_assign),
            score=score,
            reasons=tuple(reasons),
        )
