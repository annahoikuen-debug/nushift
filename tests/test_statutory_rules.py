"""statutory_rules.py の境界値テスト。"""

from datetime import time

import pytest

from shiftai.domain import Contract, EmploymentType
from shiftai.statutory_rules import (
    BAND_TOLERANCE_HOURS,
    STATUTORY_BREAK_MINUTES,
    STATUTORY_DAILY_WORK_HOURS,
    STATUTORY_MAX_DAILY_WORK_HOURS,
    STATUTORY_MIN_REST_HOURS,
    STATUTORY_WEEKLY_WORK_HOURS,
    daily_cap_minutes,
    required_break_minutes,
)


@pytest.mark.parametrize(
    "work_minutes,expected",
    [
        (0, 0),
        (360, 0),
        (361, 45),
        (480, 45),
        (481, 60),
        (600, 60),
    ],
)
def test_required_break_minutes_boundaries(work_minutes: int, expected: int) -> None:
    """労働基準法第34条に基づく必要休憩分数の境界値。"""
    assert required_break_minutes(work_minutes) == expected


def test_daily_cap_minutes_no_overtime() -> None:
    """残業不可・36協定なし → 日8hで頭打ち（480分）。"""
    c = Contract(
        daily_hours=8.0,
        overtime_allowed=False,
        has_36_agreement=False,
        employment_type=EmploymentType.SEI,
        weekly_hours=40.0,
        min_monthly_hours=60.0,
        max_monthly_hours=180.0,
        max_weekly_days=5,
        max_consecutive_days=5,
        min_rest_hours=11.0,
        granularity_min=30,
        earliest_start=time(7, 0),
        latest_end=time(20, 0),
        can_work_holiday=True,
    )
    assert daily_cap_minutes(c) == 480


def test_daily_cap_minutes_overtime_36_8h() -> None:
    """残業可+36協定あり・日8h → min(8*1.25, 10)=10h=600分。"""
    c = Contract(
        daily_hours=8.0,
        overtime_allowed=True,
        has_36_agreement=True,
        employment_type=EmploymentType.SEI,
        weekly_hours=40.0,
        min_monthly_hours=60.0,
        max_monthly_hours=180.0,
        max_weekly_days=5,
        max_consecutive_days=5,
        min_rest_hours=11.0,
        granularity_min=30,
        earliest_start=time(7, 0),
        latest_end=time(20, 0),
        can_work_holiday=True,
    )
    assert daily_cap_minutes(c) == 600


def test_daily_cap_minutes_overtime_36_9h() -> None:
    """日9h+36あり → min(9*1.25=11.25, 10)=10h=600分。"""
    c = Contract(
        daily_hours=9.0,
        overtime_allowed=True,
        has_36_agreement=True,
        employment_type=EmploymentType.SEI,
        weekly_hours=40.0,
        min_monthly_hours=60.0,
        max_monthly_hours=180.0,
        max_weekly_days=5,
        max_consecutive_days=5,
        min_rest_hours=11.0,
        granularity_min=30,
        earliest_start=time(7, 0),
        latest_end=time(20, 0),
        can_work_holiday=True,
    )
    assert daily_cap_minutes(c) == 600


def test_band_tolerance_hours() -> None:
    """月間バンド判定の許容差は 0.25h。"""
    assert BAND_TOLERANCE_HOURS == 0.25


def test_statutory_values() -> None:
    """法定定数の存在を確認。"""
    assert STATUTORY_DAILY_WORK_HOURS == 8
    assert STATUTORY_MAX_DAILY_WORK_HOURS == 10.0
    assert STATUTORY_WEEKLY_WORK_HOURS == 44
    assert STATUTORY_MIN_REST_HOURS == 11
    assert STATUTORY_BREAK_MINUTES == 45
