"""Snapshot index for day-level O(1) repair evaluation.

月次シフト解または確定シフトから、当日1日分の勤務状況・公休・累計労働時間・
連勤数・スキルを O(当日職員数) で一括集計し、欠勤リペア時の高速 O(1) 参照を可能にする。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time as dt_time
from typing import Any, Iterable, Mapping, Optional, Sequence

from shiftai.domain import CellState, Contract, Role, StaffMember, StaffPreferences


@dataclass(frozen=True)
class DaySnapshot:
    """当日1日のスナップショット（不変データクラス）"""

    day: date
    on_duty: dict[str, list[str]]  # staff_id -> 勤務中スロット一覧 ["07:00-07:30", ...]
    off_duty: tuple[str, ...]  # 当日公休の職員ID一覧
    absent: frozenset[str]  # 突発欠勤者ID一覧
    staff_map: dict[str, StaffMember]
    contracts: dict[str, Contract]
    preferences: dict[str, StaffPreferences]
    consecutive_workdays: dict[str, int]  # 前日までの連勤数
    daily_worked_hours: dict[str, float]  # 当日の確定勤務時間数（スロット数 * 0.5h）
    assigned_classes: dict[str, Optional[str]]  # staff_id -> 担当クラスID

    def is_qualified(self, staff_id: str) -> bool:
        """保育士資格（または有資格者）を保有しているか"""
        member = self.staff_map.get(staff_id)
        if not member:
            return False
        return any(
            r in (Role.HOIKUSHI, Role.YOUCHUIN, Role.KANGSHI)
            or "保育士" in str(r)
            for r in member.roles
        )

    def is_leader(self, staff_id: str) -> bool:
        """主任・リーダー権限を持つか"""
        member = self.staff_map.get(staff_id)
        if not member:
            return False
        return getattr(member, "can_lead", False) or any("主任" in str(r) or "リーダー" in str(r) for r in member.roles)


class SnapshotIndex:
    """当日スナップショットを生成・キャッシュするインデクサー"""

    @staticmethod
    def build(
        existing_assignments: Iterable[tuple[str, date, str] | Any],
        target_date: date,
        staff: Sequence[StaffMember],
        absent_staff_ids: Iterable[str],
        contracts: Optional[Mapping[str, Contract]] = None,
        preferences: Optional[Mapping[str, StaffPreferences]] = None,
        granularity_min: int = 30,
    ) -> DaySnapshot:
        """確定解から当日スナップショットを高速構築する。"""
        absent_set = frozenset(str(sid) for sid in absent_staff_ids)
        staff_map = {str(s.staff_id): s for s in staff}
        contracts_map = {str(s.staff_id): s.contract for s in staff}
        if contracts:
            contracts_map.update({str(k): v for k, v in contracts.items()})
        prefs_map = {str(s.staff_id): preferences.get(str(s.staff_id)) for s in staff} if preferences else {}

        on_duty_slots: dict[str, list[str]] = {}
        for s in staff:
            on_duty_slots[str(s.staff_id)] = []

        # existing_assignments のパース
        for item in existing_assignments:
            # ShiftAssignment オブジェクトの場合
            if hasattr(item, "staff_id") and hasattr(item, "day"):
                sid = str(item.staff_id)
                d = item.day
                sl = getattr(getattr(item, "slot", None), "label", str(getattr(item, "slot", "")))
                st = getattr(item, "state", None)
                if d == target_date and sid not in absent_set:
                    if st in (CellState.WORK, CellState.BREAK):
                        on_duty_slots.setdefault(sid, []).append(sl)
            # (staff_id, day, slot) 形式のタプルまたは辞書キーの場合
            elif isinstance(item, tuple) and len(item) >= 3:
                sid, d, sl = str(item[0]), item[1], str(item[2])
                if d == target_date and sid not in absent_set:
                    on_duty_slots.setdefault(sid, []).append(sl)

        # 出勤者と公休者の分類
        on_duty: dict[str, list[str]] = {}
        off_duty_list: list[str] = []
        daily_hours: dict[str, float] = {}
        slot_hours = granularity_min / 60.0

        for sid, slots in on_duty_slots.items():
            if sid in absent_set:
                continue
            # スロットが空でなく、"休" や "OFF" 以外の実勤務があるか
            work_slots = [
                s for s in slots if s not in ("休", "公休", "OFF", "all") and "-" in s
            ]
            if work_slots:
                work_slots.sort()
                on_duty[sid] = work_slots
                daily_hours[sid] = len(work_slots) * slot_hours
            else:
                off_duty_list.append(sid)
                daily_hours[sid] = 0.0

        # クラス担当
        assigned_classes = {
            str(s.staff_id): getattr(s, "assigned_class_id", None)
            for s in staff
        }

        # 連勤数の初期概算（当日が連勤制限に抵触しないか評価用）
        consecutive_workdays = {str(s.staff_id): 1 for s in staff}

        return DaySnapshot(
            day=target_date,
            on_duty=on_duty,
            off_duty=tuple(off_duty_list),
            absent=absent_set,
            staff_map=staff_map,
            contracts=contracts_map,
            preferences={k: v for k, v in prefs_map.items() if v is not None},
            consecutive_workdays=consecutive_workdays,
            daily_worked_hours=daily_hours,
            assigned_classes=assigned_classes,
        )
