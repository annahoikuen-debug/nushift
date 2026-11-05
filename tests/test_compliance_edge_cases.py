"""Boundary value and edge case tests for compliance, 36 agreement, and variable hours."""

from __future__ import annotations

from datetime import date, time

from shiftai.domain import (
    AgeClass,
    CellState,
    Contract,
    EmploymentType,
    Requirement,
    RequirementTable,
    Role,
    ShiftAssignment,
    ShiftDay,
    Slot,
    SolveResult,
    SolveStatus,
    StaffMember,
    ViolationSeverity,
)
from shiftai.gap_analysis import check_violations


def _make_slot(h: int, m: int = 0) -> Slot:
    end_h = h if m == 0 else h + 1
    end_m = 30 if m == 0 else 0
    return Slot(time(h, m), time(end_h, end_m))


def test_statutory_daily_hours_blocker_gate():
    """Verify that daily work hours exceeding contractual/legal cap triggers violation."""
    day = date(2026, 10, 5)
    slots = [_make_slot(7 + i // 2, 30 if i % 2 else 0) for i in range(24)]  # 12 hours
    req_rows = [
        Requirement(
            day=day,
            slot=slot,
            age_class=AgeClass.INFANT,
            child_count=3,
            needed_staff=1,
            needed_qualified=1,
        )
        for slot in slots
    ]
    table = RequirementTable(
        day_open=time(7, 0),
        day_close=time(19, 0),
        granularity_min=30,
        slots=tuple(slots),
        rows={day: req_rows},
    )

    staff = [
        StaffMember(
            staff_id="S01",
            name="テスト保育士",
            roles=(Role.HOIKUSHI,),
            contract=Contract(
                weekly_hours=40.0,
                daily_hours=8.0,
                employment_type=EmploymentType.SEI,
                overtime_allowed=True,
            ),
        )
    ]

    # Assign 10.5 hours of continuous WORK (21 slots of 30min)
    assignments = [
        ShiftAssignment(staff_id="S01", day=day, slot=slots[i], state=CellState.WORK)
        for i in range(21)
    ] + [
        ShiftAssignment(staff_id="S01", day=day, slot=slots[i], state=CellState.OFF)
        for i in range(21, 24)
    ]

    res = SolveResult(
        status=SolveStatus.FEASIBLE,
        shift_days=[ShiftDay(day=day, assignments={("S01", s.label): a.state for s, a in zip(slots, assignments, strict=False)})],
        assignments=assignments,
        objective_value=100.0,
    )

    violations = check_violations(table, res, staff)
    excess_violations = [v for v in violations if v.code in ("DAILY_HOURS_EXCEEDED", "OVERTIME_NOT_ALLOWED")]
    assert len(excess_violations) > 0
    assert any(v.severity in (ViolationSeverity.WARNING, ViolationSeverity.BLOCKER) for v in excess_violations)
