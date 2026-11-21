"""Candidate evaluation: Hard filters and soft scoring for repair actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Sequence

from shiftai.domain import PairingRule
from .gap_scanner import SlotGap
from .snapshot import DaySnapshot


class ActionType(str, Enum):
    """リペア行動の種類"""
    EXTENSION = "EXTENSION"          # 当日出勤中職員の勤務時間延長
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


class CandidateEvaluator:
    """候補職員の適格性判定（ハードフィルタ）および優先度スコア計算器"""

    @staticmethod
    def evaluate(
        snapshot: DaySnapshot,
        gap: SlotGap,
        candidate_id: str,
        slots_to_assign: Sequence[str],
        action_type: ActionType,
        pairing_rules: Optional[Sequence[PairingRule]] = None,
        max_daily_hours: float = 10.0,
    ) -> Optional[CandidateAction]:
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

        # H6 & H8: 1日10時間上限（労基法第32条）および連勤チェック
        current_hours = snapshot.daily_worked_hours.get(candidate_id, 0.0)
        additional_hours = len(slots_to_assign) * 0.5
        new_total_hours = current_hours + additional_hours

        if new_total_hours > max_daily_hours:
            return None

        # 契約上限（週40時間／契約上限）の簡易チェック
        contract = snapshot.contracts.get(candidate_id)
        if contract and hasattr(contract, "max_consecutive_days"):
            if snapshot.consecutive_workdays.get(candidate_id, 0) >= contract.max_consecutive_days:
                if action_type == ActionType.HELP_OFF_DUTY:
                    return None

        # H5: ペアリング制約（禁止ペア相手と同時勤務にならないか）
        if pairing_rules:
            on_duty_staff = set(snapshot.on_duty.keys())
            for pr in pairing_rules:
                if getattr(pr, "is_avoid", False) or getattr(pr, "rule_type", "") == "avoid":
                    pair_members = set(getattr(pr, "staff_ids", ()))
                    if candidate_id in pair_members:
                        other_members = pair_members - {candidate_id}
                        if other_members & on_duty_staff:
                            # 禁止ペアが当日出勤中なら除外
                            return None

        # --- ソフトスコア計算 ---
        score = 0.0
        reasons: list[str] = []

        # 1. 行動種別スコア（延長は公休応援より高評価）
        if action_type == ActionType.EXTENSION:
            score += 30.0
            reasons.append("当日出勤者による勤務時間延長 (+30)")
        else:
            score -= 50.0
            reasons.append("公休からの応援出勤 (-50)")

        # 2. クラス一致ボーナス
        staff_class = snapshot.assigned_classes.get(candidate_id)
        if gap.class_id and staff_class:
            if staff_class == gap.class_id:
                score += 40.0
                reasons.append(f"同クラス({gap.class_id})担当継続 (+40)")
            else:
                score -= 15.0
                reasons.append("他クラスへの応援 (-15)")

        # 3. 残業余力スコア（当日の勤務時間が短い職員を優先）
        if current_hours <= 4.0:
            score += 15.0
            reasons.append("当日の勤務時間に余裕あり (+15)")
        elif current_hours >= 8.0:
            score -= 10.0
            reasons.append("実働8h超過残業 (-10)")

        # 4. 有資格者ボーナス
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
