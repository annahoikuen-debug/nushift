"""Snapshot index for day-level O(1) repair evaluation.

月次シフト解または確定シフトから、当日1日分の勤務状況・公休・累計労働時間・
連勤数・スキルを O(当日職員数) で一括集計し、欠勤リペア時の高速 O(1) 参照を可能にする。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

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
    daily_worked_hours: dict[str, float]  # 当日の確定勤務時間数
    assigned_classes: dict[str, str | None]  # staff_id -> 担当クラスID

    def is_qualified(self, staff_id: str) -> bool:
        """保育士資格（または有資格者）を保有しているか"""
        member = self.staff_map.get(staff_id)
        if not member:
            return False
        return any(
            r in (Role.HOIKUSHI, Role.YOUCHUIN, Role.KANGSHI) or "保育士" in str(r)
            for r in member.roles
        )

    def is_leader(self, staff_id: str) -> bool:
        """主任・リーダー権限を持つか"""
        member = self.staff_map.get(staff_id)
        if not member:
            return False
        return getattr(member, "can_lead", False) or any(
            "主任" in str(r) or "リーダー" in str(r) for r in member.roles
        )


def _count_prior_consecutive_days(worked_dates: set[date], target_date: date) -> int:
    """target_date 直前日までの連続勤務日数を計算する。"""
    streak = 0
    cursor = target_date - timedelta(days=1)
    while cursor in worked_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


class SnapshotIndex:
    """当日スナップショットを生成・キャッシュするインデクサー"""

    @staticmethod
    def build(
        existing_assignments: Iterable[tuple[str, date, str] | Any],
        target_date: date,
        staff: Sequence[StaffMember],
        absent_staff_ids: Iterable[str],
        contracts: Mapping[str, Contract] | None = None,
        preferences: Mapping[str, StaffPreferences] | None = None,
        granularity_min: int = 30,
    ) -> DaySnapshot:
        """確定解から当日スナップショットを高速構築する。"""
        absent_set = frozenset(str(sid) for sid in absent_staff_ids)
        staff_map = {str(s.staff_id): s for s in staff}
        contracts_map = {str(s.staff_id): s.contract for s in staff}
        if contracts:
            contracts_map.update({str(k): v for k, v in contracts.items()})
        prefs_map = (
            {str(s.staff_id): preferences.get(str(s.staff_id)) for s in staff}
            if preferences
            else {}
        )

        on_duty_slots: dict[str, list[str]] = {str(s.staff_id): [] for s in staff}
        worked_dates_by_staff: dict[str, set[date]] = {str(s.staff_id): set() for s in staff}

        # existing_assignments のパース
        # 辞書形式で渡された場合は (sid, day, slot) -> CellState のみ実勤務を抽出
        is_mapping = isinstance(existing_assignments, Mapping)
        items = existing_assignments.items() if is_mapping else existing_assignments

        for entry in items:
            if is_mapping:
                key, st = entry
                if not (isinstance(key, tuple) and len(key) >= 3):
                    continue
                sid, d, sl = str(key[0]), key[1], str(key[2])
                if st not in (CellState.WORK, CellState.BREAK):
                    continue
            elif hasattr(entry, "staff_id") and hasattr(entry, "day"):
                sid = str(entry.staff_id)
                d = entry.day
                sl = getattr(getattr(entry, "slot", None), "label", str(getattr(entry, "slot", "")))
                st = getattr(entry, "state", None)
                if st not in (CellState.WORK, CellState.BREAK):
                    continue
            elif isinstance(entry, tuple) and len(entry) >= 3:
                sid, d, sl = str(entry[0]), entry[1], str(entry[2])
            else:
                continue

            if sl not in ("休", "公休", "OFF", "all") and "-" in sl:
                if d < target_date:
                    worked_dates_by_staff.setdefault(sid, set()).add(d)
                elif d == target_date and sid not in absent_set:
                    on_duty_slots.setdefault(sid, []).append(sl)

        # 出勤者と公休者の分類
        on_duty: dict[str, list[str]] = {}
        off_duty_list: list[str] = []
        daily_hours: dict[str, float] = {}
        slot_hours = granularity_min / 60.0

        for sid, slots in on_duty_slots.items():
            if sid in absent_set:
                continue
            work_slots = sorted(
                {s for s in slots if s not in ("休", "公休", "OFF", "all") and "-" in s}
            )
            if work_slots:
                on_duty[sid] = work_slots
                daily_hours[sid] = len(work_slots) * slot_hours
            else:
                off_duty_list.append(sid)
                daily_hours[sid] = 0.0

        assigned_classes = {str(s.staff_id): getattr(s, "assigned_class_id", None) for s in staff}

        consecutive_workdays = {
            str(s.staff_id): _count_prior_consecutive_days(
                worked_dates_by_staff.get(str(s.staff_id), set()), target_date
            )
            for s in staff
        }

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
