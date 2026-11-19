"""Timefold Shadow Variables 思想に基づくリアクティブ状態管理エンジン。

シフト変数の決定・変更に連動して、累計実働時間・連続勤務日数・勤務間インターバル
などの従属状態を定数時間 O(1) で即座に追従・照会できるようにする。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from shiftai.domain import CellState, Slot


@dataclass(frozen=True)
class StaffDailyState:
    """職員の1日あたりの従属勤務状態（シャドウ変数）。"""

    work_minutes: int = 0
    break_minutes: int = 0
    first_start_minutes: int | None = None
    last_end_minutes: int | None = None

    @property
    def is_working(self) -> bool:
        """当日勤務があるか。"""
        return self.work_minutes > 0


class ShadowStateTracker:
    """シフト割当の変更をリアクティブに反映し、制約判定に必要な状態を O(1) で提供するトラッカー。"""

    def __init__(self, slots: Sequence[Slot] | None = None) -> None:
        self._slots: tuple[Slot, ...] = tuple(slots or ())
        self._slot_by_label: dict[str, Slot] = {s.label: s for s in self._slots}
        # (staff_id, day) -> StaffDailyState
        self._daily_states: dict[tuple[str, date], StaffDailyState] = {}
        # staff_id -> 総累計勤務分数
        self._total_work_minutes: dict[str, int] = defaultdict(int)

    def set_day_assignment(
        self,
        staff_id: str,
        day: date,
        slot_map: Mapping[str, CellState],
    ) -> StaffDailyState:
        """職員の指定日のスロット割り当てを登録し、従属状態をリアクティブに更新する。"""
        old_state = self._daily_states.get((staff_id, day))
        if old_state is not None:
            self._total_work_minutes[staff_id] -= old_state.work_minutes

        work_m = 0
        break_m = 0
        first_start: int | None = None
        last_end: int | None = None

        for label, state in slot_map.items():
            sl = self._slot_by_label.get(label)
            if sl is None:
                continue
            if state is CellState.WORK:
                work_m += sl.minutes
                if first_start is None or sl.start_minutes < first_start:
                    first_start = sl.start_minutes
                if last_end is None or sl.end_minutes > last_end:
                    last_end = sl.end_minutes
            elif state is CellState.BREAK:
                break_m += sl.minutes

        new_state = StaffDailyState(
            work_minutes=work_m,
            break_minutes=break_m,
            first_start_minutes=first_start,
            last_end_minutes=last_end,
        )
        self._daily_states[(staff_id, day)] = new_state
        self._total_work_minutes[staff_id] += work_m
        return new_state

    def get_daily_state(self, staff_id: str, day: date) -> StaffDailyState:
        """指定日の職員状態を取得する。未登録なら全OFF状態を返す。"""
        return self._daily_states.get((staff_id, day), StaffDailyState())

    def total_work_hours(self, staff_id: str) -> float:
        """職員の期間内累計実働時間（時間単位）。"""
        return round(self._total_work_minutes.get(staff_id, 0) / 60.0, 2)

    def consecutive_work_days(self, staff_id: str, target_day: date) -> int:
        """target_day を末尾とする連続勤務日数を返す（当日常態含む）。"""
        st = self.get_daily_state(staff_id, target_day)
        if not st.is_working:
            return 0
        streak = 1
        cur = target_day - timedelta(days=1)
        while self.get_daily_state(staff_id, cur).is_working:
            streak += 1
            cur -= timedelta(days=1)
        return streak

    def rest_interval_hours(self, staff_id: str, target_day: date) -> float | None:
        """前日の勤務終了から当日の勤務開始までのインターバル休息時間（時間単位）。

        前日または当日に勤務がない場合は None を返す。
        """
        today_st = self.get_daily_state(staff_id, target_day)
        if not today_st.is_working or today_st.first_start_minutes is None:
            return None
        prev_day = target_day - timedelta(days=1)
        prev_st = self.get_daily_state(staff_id, prev_day)
        if not prev_st.is_working or prev_st.last_end_minutes is None:
            return None

        # インターバル分数 = (24 * 60 - 前日終了時刻) + 当日開始時刻
        interval_m = (1440 - prev_st.last_end_minutes) + today_st.first_start_minutes
        return round(interval_m / 60.0, 2)
