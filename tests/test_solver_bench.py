"""Benchmark harness for solver performance and scalability measurements."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from shiftai.data_loader import load_children, load_preferences, load_staff, read_table
from shiftai.domain import FacilitySettings
from shiftai.local_rules import get_standard
from shiftai.solver import solve_shift
from shiftai.standards import build_requirements


@pytest.mark.benchmark
def test_benchmark_sample_solve_time():
    """Measure solve time on standard sample data to detect performance regressions."""
    sample_dir = Path(__file__).resolve().parent.parent / "sample"
    c_df = read_table(sample_dir / "children.csv", "children")
    s_df = read_table(sample_dir / "staff.csv", "staff")
    p_df = read_table(sample_dir / "preferences.csv", "preferences")

    children, _ = load_children(c_df)
    staff, _ = load_staff(s_df)
    prefs, _ = load_preferences(p_df)

    settings = FacilitySettings()
    standard = get_standard("全国基準（厚労省）")
    days = sorted(list({c.day for c in children}))[:2]
    children_sub = [c for c in children if c.day in days]

    reqs = build_requirements(
        children_sub,
        days=days,
        standard=standard,
        day_open=settings.day_open,
        day_close=settings.day_close,
        closed_days=settings.closed_days,
        holiday_dates=settings.holiday_dates,
    )

    t0 = time.perf_counter()
    result = solve_shift(
        children=children_sub,
        requirements=reqs,
        staff=staff,
        preferences=prefs,
        settings=settings,
        standard=standard,
        time_limit_sec=20,
    )
    elapsed = time.perf_counter() - t0

    assert result.status.value in ("最適解", "実行可能解")
    # Regression threshold: sample solve for 2 days should complete within 25 seconds
    assert elapsed < 25.0, f"Solver regression detected: took {elapsed:.2f}s"

