"""Scanner to detect shortfall gaps caused by absences."""

from __future__ import annotations

from dataclasses import dataclass

from shiftai.domain import RequirementTable

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
    class_id: str | None = None

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

        req_slots = requirements.slots if hasattr(requirements, "slots") else ()
        req_rows = (
            requirements.for_day(target_date)
            if hasattr(requirements, "for_day")
            else getattr(requirements, "rows", {}).get(target_date, [])
        )

        # rows 側をスロットラベルで年齢クラス横断合算（mock/簡易表フォールバック用）
        rows_by_label: dict[str, tuple[int, int]] = {}
        for r in req_rows:
            s_lbl = r.slot.label if hasattr(r, "slot") else str(r)
            n_st = int(getattr(r, "needed_staff", 0))
            n_ql = int(getattr(r, "needed_qualified", 0))
            prev_st, prev_ql = rows_by_label.get(s_lbl, (0, 0))
            rows_by_label[s_lbl] = (prev_st + n_st, prev_ql + n_ql)

        has_req_methods = hasattr(requirements, "needed_staff") and hasattr(
            requirements, "needed_qualified"
        )

        for slot_obj in req_slots:
            s_label = slot_obj.label if hasattr(slot_obj, "label") else str(slot_obj)
            if has_req_methods and hasattr(slot_obj, "start"):
                need_total = int(requirements.needed_staff(target_date, slot_obj))
                need_qualified = int(requirements.needed_qualified(target_date, slot_obj))
            else:
                need_total, need_qualified = rows_by_label.get(s_label, (0, 0))

            if need_total <= 0 and need_qualified <= 0:
                continue

            # 現時点で出勤している職員数（欠勤者は snapshot.on_duty から除外済み）
            on_duty_staff = [sid for sid, s_list in snapshot.on_duty.items() if s_label in s_list]
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
