"""stats の真実性（solver 名・elapsed_sec・pass・ギャップ記録）の回帰テスト。"""
from datetime import date, timedelta

from shiftai import local_rules, sample_data
from shiftai.config import DEFAULT_DAY_OPEN, DEFAULT_DAY_CLOSE, DEFAULT_GRANULARITY_MIN
from shiftai.domain import FacilitySettings
from shiftai.standards import build_requirements
from shiftai.solver import solve_shift

STANDARD = local_rules.get_standard("全国基準（厚労省）")


def _solve():
    days = [date(2026, 11, 16) + timedelta(days=i) for i in range(7)]
    children, staff, prefs = sample_data.make_dataset(days, seed=42)
    settings = FacilitySettings()
    table = build_requirements(
        children, days, STANDARD,
        day_open=DEFAULT_DAY_OPEN, day_close=DEFAULT_DAY_CLOSE,
        granularity_min=DEFAULT_GRANULARITY_MIN,
    )
    return solve_shift(
        children, staff, table, preferences=prefs, settings=settings,
        time_limit_sec=30, standard=STANDARD,
    )


def test_greedy_fallback_records_elapsed_and_pass():
    """貪欲法フォールバック時に elapsed_sec/pass が記録されること。"""
    res = _solve()
    s = res.stats
    if s.get("fallback") == "greedy" or s.get("solver") == "greedy":
        assert s.get("elapsed_sec", 0) > 0
        assert s.get("pass") == 0
    else:
        assert s.get("elapsed_sec", 0) > 0
        assert s.get("pass") in (1, 2)


def test_stats_has_mip_info_keys_when_highs():
    """HiGHS 使用時に mip_gap/mip_dual_bound/mip_node_count が記録されること。"""
    res = _solve()
    s = res.stats
    if s.get("solver") == "HiGHS":
        assert "mip_gap" in s
        assert "mip_dual_bound" in s
        assert "mip_node_count" in s
