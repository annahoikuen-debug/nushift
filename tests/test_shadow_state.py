"""Timefold Shadow Variables (ShadowStateTracker) の単体テスト。"""

from __future__ import annotations

from datetime import date, time

from shiftai.domain import CellState, Slot
from shiftai.shadow_state import ShadowStateTracker


def test_shadow_state_tracker():
    """シャドウ変数が累計時間・連続勤務・インターバルを正確に追従するか検証。"""
    s1 = Slot(start=time(9, 0), end=time(13, 0))  # 09:00-13:00 (4h)
    s2 = Slot(start=time(13, 0), end=time(18, 0))  # 13:00-18:00 (5h)
    s_break = Slot(start=time(12, 0), end=time(13, 0))  # 休憩1h
    slots = [s1, s2, s_break]
    tracker = ShadowStateTracker(slots)

    d1 = date(2026, 4, 1)
    d2 = date(2026, 4, 2)
    d3 = date(2026, 4, 3)

    # Day 1: 09:00-18:00 (実働8h, 休憩1h)
    tracker.set_day_assignment(
        "S001",
        d1,
        {s1.label: CellState.WORK, s2.label: CellState.WORK, s_break.label: CellState.BREAK},
    )
    # Day 2: 13:00-18:00 (実働5h)
    tracker.set_day_assignment(
        "S001",
        d2,
        {s1.label: CellState.OFF, s2.label: CellState.WORK},
    )

    # 1. 累計実働時間
    # s1(4h) + s2(5h) = 9h on d1, s2(5h) on d2 -> 14h
    assert tracker.total_work_hours("S001") == 14.0

    # 2. 連続勤務日数
    assert tracker.consecutive_work_days("S001", d1) == 1
    assert tracker.consecutive_work_days("S001", d2) == 2
    assert tracker.consecutive_work_days("S001", d3) == 0  # d3は未勤務

    # 3. 勤務間インターバル時間
    # d1 終了: 18:00 (1080m) -> d2 開始: 13:00 (780m)
    # インターバル = (1440 - 1080) + 780 = 360 + 780 = 1140m = 19.0時間
    interval = tracker.rest_interval_hours("S001", d2)
    assert interval == 19.0

    # 4. リアクティブな変更追従（d1 を短縮した場合）
    tracker.set_day_assignment(
        "S001",
        d1,
        {s1.label: CellState.WORK, s2.label: CellState.OFF},
    )
    # d1 が 4h になったため、総時間は 4h + 5h = 9.0h に減る
    assert tracker.total_work_hours("S001") == 9.0
    # d1 終了が 13:00 (780m) になったため、インターバルは (1440 - 780) + 780 = 1440m = 24.0時間
    assert tracker.rest_interval_hours("S001", d2) == 24.0
