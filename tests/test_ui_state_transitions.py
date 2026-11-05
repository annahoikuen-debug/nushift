"""State integrity and transition tests for UI session state management."""

from __future__ import annotations

from datetime import date

from shiftai.domain import CellState
from shiftai.ui import state


def test_ui_state_fixed_assignments_normalization():
    """Verify that normalize_fixed properly converts raw tuples/dicts into canonical form."""
    raw = {
        ("S01", "2026-10-02", "09:00"): "勤務",
        ("S02", date(2026, 10, 2), "10:00"): CellState.BREAK,
    }
    normalized = state.normalize_fixed(raw)

    assert isinstance(normalized, dict)
    assert ("S01", date(2026, 10, 2), "09:00") in normalized
    assert normalized[("S01", date(2026, 10, 2), "09:00")] is CellState.WORK
    assert normalized[("S02", date(2026, 10, 2), "10:00")] is CellState.BREAK


def test_ui_state_reset_and_defaults():
    """Verify default facility settings fallback when state is uninitialized."""
    settings = state.current_settings()
    assert settings.facility_name != ""
    assert settings.day_open < settings.day_close

