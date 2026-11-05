"""Golden Master snapshot generation and verification tests for NuShift solver."""

from __future__ import annotations

import json
from pathlib import Path

from shiftai.data_loader import load_children, load_preferences, load_staff, read_table
from shiftai.domain import FacilitySettings, SolveResult, SolveStatus
from shiftai.local_rules import get_standard
from shiftai.solver import solve_shift
from shiftai.standards import build_requirements

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "golden_master"
SNAPSHOT_FILE = FIXTURES_DIR / "sample_solver_snapshot.json"


def _solve_sample() -> SolveResult:
    sample_dir = Path(__file__).resolve().parent.parent / "sample"
    c_df = read_table(sample_dir / "children.csv", "children")
    s_df = read_table(sample_dir / "staff.csv", "staff")
    p_df = read_table(sample_dir / "preferences.csv", "preferences")

    children, _ = load_children(c_df)
    staff, _ = load_staff(s_df)
    prefs, _ = load_preferences(p_df)

    settings = FacilitySettings()
    standard = get_standard("全国基準（厚労省）")
    days = sorted(list({c.day for c in children}))[:2]  # First 2 days for deterministic snapshot
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

    result = solve_shift(
        children=children_sub,
        requirements=reqs,
        staff=staff,
        preferences=prefs,
        settings=settings,
        standard=standard,
        time_limit_sec=20,
    )
    return result


def _serialize_result(res: SolveResult) -> dict:
    is_feasible = res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    return {
        "status": res.status.value,
        "is_feasible": is_feasible,
        "objective_value": round(res.objective_value, 2) if res.objective_value is not None else None,
        "total_assignments": len(res.assignments),
        "work_count": sum(1 for a in res.assignments if a.state.value == "勤務"),
        "break_count": sum(1 for a in res.assignments if a.state.value == "休憩"),
        "off_count": sum(1 for a in res.assignments if a.state.value == "オフ"),
        "violations": [
            {
                "code": v.code,
                "severity": v.severity.value,
                "staff_id": v.staff_id,
                "day": v.day.isoformat() if v.day else None,
            }
            for v in res.violations
        ],
    }


def test_generate_or_verify_golden_master():
    """Verify that solver output matches the frozen golden master snapshot."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    res = _solve_sample()
    current_data = _serialize_result(res)

    if not SNAPSHOT_FILE.exists():
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(current_data, f, ensure_ascii=False, indent=2)
        assert current_data["total_assignments"] > 0
        return

    with open(SNAPSHOT_FILE, encoding="utf-8") as f:
        expected_data = json.load(f)

    assert current_data["status"] == expected_data["status"]
    assert current_data["is_feasible"] == expected_data["is_feasible"]
    assert current_data["work_count"] == expected_data["work_count"]
    assert current_data["break_count"] == expected_data["break_count"]
    assert len(current_data["violations"]) == len(expected_data["violations"])
