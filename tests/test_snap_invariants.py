"""Invariants and safety properties tests for snap_to_patterns post-processing."""

from __future__ import annotations

from pathlib import Path

from shiftai.data_loader import load_children, load_preferences, load_staff, read_table
from shiftai.domain import FacilitySettings
from shiftai.local_rules import get_standard
from shiftai.shift_patterns import default_patterns
from shiftai.solver import snap_to_patterns, solve_shift
from shiftai.standards import build_requirements


def test_snap_to_patterns_does_not_increase_blocker_violations():
    """Property test: post-processing snap_to_patterns must never introduce new BLOCKER violations."""
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

    raw_result = solve_shift(
        children=children_sub,
        requirements=reqs,
        staff=staff,
        preferences=prefs,
        settings=settings,
        standard=standard,
        time_limit_sec=20,
    )

    patterns = default_patterns()
    snapped_result, changes = snap_to_patterns(raw_result, patterns, settings=settings)

    raw_blockers = [v for v in raw_result.violations if v.severity.value == "法令違反"]
    snapped_blockers = [v for v in snapped_result.violations if v.severity.value == "法令違反"]

    # Invariant: snap must not introduce additional statutory violations
    assert len(snapped_blockers) <= len(raw_blockers), (
        f"Post-processing increased BLOCKER violations from {len(raw_blockers)} to {len(snapped_blockers)}"
    )

    # Invariant: total assigned cell count must remain identical
    assert len(snapped_result.assignments) == len(raw_result.assignments)
