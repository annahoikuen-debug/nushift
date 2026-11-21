"""Scanner to detect shortfall gaps caused by absences."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from shiftai.domain import RequirementTable, Slot
from .snapshot import DaySnapshot


@dataclass(frozen=True)
class SlotGap:
    """不足スロットの定義"""

    slot_label: str
    need_total: int
    have_total: int
    need_qualified: int
    have_qualified: int
    gap_total: int
    gap_qualified: int
    require_leader: bool = False
    class_id: Optional[str] = None

    @property
    def is_critical(self) -> bool:
        """配置割れ（監査返還リスク）が発生しているか"""
        return self.gap_total > 0 or self.gap_qualified > 0


class SlotGapScanner:
    """当日スナップショットと配置基準から不足スロットを走査・検出するスキャナー"""

    @staticmethod
    def scan(
        snapshot: DaySnapshot,
        requirements: RequirementTable,
        require_leader: bool = False,
    ) -> list[SlotGap]:
        """当日全スロットの過不足を走査し、不足（Gap）一覧を返却する。"""
        gaps: list[SlotGap] = []
        target_date = snapshot.day

        # requirements.slots または rows から走査
        req_slots = requirements.slots if hasattr(requirements, "slots") else ()
        req_rows = getattr(requirements, "rows", {}).get(target_date, [])
        req_by_slot = {r.slot.label if hasattr(r, "slot") else str(r): r for r in req_rows}

        for slot_obj in req_slots:
            s_label = slot_obj.label if hasattr(slot_obj, "label") else str(slot_obj)
            req_item = req_by_slot.get(s_label)

            need_total = getattr(req_item, "total", 2) if req_item else 2
            need_qualified = getattr(req_item, "qualified", 1) if req_item else 1

            # 現時点で出勤している職員数（欠勤者は snapshot.on_duty から除外済み）
            on_duty_staff = [
                sid
                for sid, s_list in snapshot.on_duty.items()
                if s_label in s_list
            ]
            have_total = len(on_duty_staff)
            have_qualified = sum(1 for sid in on_duty_staff if snapshot.is_qualified(sid))

            gap_total = max(0, need_total - have_total)
            gap_qualified = max(0, need_qualified - have_qualified)

            if gap_total > 0 or gap_qualified > 0:
                gaps.append(
                    SlotGap(
                        slot_label=s_label,
                        need_total=need_total,
                        have_total=have_total,
                        need_qualified=need_qualified,
                        have_qualified=have_qualified,
                        gap_total=gap_total,
                        gap_qualified=gap_qualified,
                        require_leader=require_leader,
                    )
                )

        return gaps
