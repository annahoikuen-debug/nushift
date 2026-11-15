"""パターン文庫（_ensure_effective_patterns）の回帰テスト。

K1 未達の根因であった「14:15 と 18:15 を同一職員日でカバーできる
5h パターンが存在しない」問題を、ブリッジパターン part_ml(14:00-19:00)
によって解消していることを将来にわたって保証する。
"""

import datetime as dt
from datetime import date

from shiftai import local_rules, sample_data
from shiftai.config import (
    DEFAULT_DAY_CLOSE,
    DEFAULT_DAY_OPEN,
    DEFAULT_GRANULARITY_MIN,
)
from shiftai.domain import FacilitySettings
from shiftai.monthly import MonthlySolveContext, _ensure_effective_patterns
from shiftai.standards import build_requirements

STANDARD = local_rules.get_standard("全国基準（厚労省）")


def test_ブリッジパターンが午後と夕方を同時にカバーする():
    """part_ml(14:00-19:00) は 14:15 と 18:15 の両方を含むこと。

    14:00-14:30 と 18:00-18:30 を同一職員日でカバーできる 5h パターンが
    存在しないことが K1 未達の根因であり、この回帰を将来にわたって保証する。
    """
    days = [date(2026, 11, 16) + dt.timedelta(days=i) for i in range(1)]
    children, staff, prefs = sample_data.make_dataset(days, seed=42)
    table = build_requirements(
        children,
        days,
        STANDARD,
        day_open=DEFAULT_DAY_OPEN,
        day_close=DEFAULT_DAY_CLOSE,
        granularity_min=DEFAULT_GRANULARITY_MIN,
    )
    ctx = MonthlySolveContext(
        target_month="2026-11",
        all_days=days,
        staff=staff,
        contracts={s.staff_id: s.contract for s in staff if s.contract},
        preferences=prefs,
        patterns=[],
        requirements=table,
        facility_settings=FacilitySettings(),
        fixed_assignments={},
        prior_duty_history={},
        monthly_holiday_target=8,
        children=children,
        standard=STANDARD,
    )
    patterns = {p.key: p for p in _ensure_effective_patterns(ctx)}

    assert "part_ml" in patterns
    bridge = patterns["part_ml"]
    assert bridge.start_minutes <= 14 * 60 + 15 < bridge.end_minutes
    assert bridge.start_minutes <= 18 * 60 + 15 < bridge.end_minutes
    assert bridge.end_minutes - bridge.start_minutes == 5 * 60
