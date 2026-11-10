"""週44時間目安の目的関数項の回帰テスト（R2-SOL-02 修正検証）。"""

from __future__ import annotations

from datetime import date, time

from shiftai.domain import (
    AgeClass,
    Contract,
    EmploymentType,
    FacilitySettings,
    ObjectiveWeights,
    Requirement,
    RequirementTable,
    Role,
    Slot,
    SlotKind,
    StaffMember,
)
from shiftai.solver import solve_shift


def test_legal_two_week_shift_is_not_penalized_for_weekly_hours():
    """14日計画・週40h（5日×8h）の合法シフトで、週44h由来のペナルティが0であること。"""
    days = [date(2026, 11, 2 + i) for i in range(14)]
    slots = (
        Slot(start=time(9, 0), end=time(13, 0)),
        Slot(start=time(14, 0), end=time(18, 0)),
    )
    rows: dict[date, list[Requirement]] = {}
    for d in days:
        rows[d] = [
            Requirement(
                day=d,
                slot=slots[0],
                age_class=AgeClass.AGE_3,
                child_count=5,
                needed_staff=1,
                needed_qualified=1,
                slot_kind=SlotKind.NORMAL,
            ),
            Requirement(
                day=d,
                slot=slots[1],
                age_class=AgeClass.AGE_3,
                child_count=5,
                needed_staff=1,
                needed_qualified=1,
                slot_kind=SlotKind.NORMAL,
            ),
        ]
    table = RequirementTable(
        day_open=time(9, 0),
        day_close=time(18, 0),
        granularity_min=240,
        slots=slots,
        rows=rows,
    )
    contract = Contract(
        employment_type=EmploymentType.SEI,
        daily_hours=8.0,
        weekly_hours=40.0,
        overtime_allowed=True,
        has_36_agreement=True,
    )
    staff = [
        StaffMember(staff_id="S01", name="職員1", roles=(Role.HOIKUSHI,), contract=contract),
        StaffMember(staff_id="S02", name="職員2", roles=(Role.HOIKUSHI,), contract=contract),
    ]
    settings = FacilitySettings(
        day_open=time(9, 0),
        day_close=time(18, 0),
        granularity_min=240,
        closed_days=frozenset(),
        holiday_dates=frozenset(),
    )

    weights_zero = ObjectiveWeights(max_shift_length_penalty=0.0)
    weights_penalty = ObjectiveWeights(max_shift_length_penalty=6.0)

    res_zero = solve_shift(
        children=[],
        staff=staff,
        requirements=table,
        settings=settings,
        weights=weights_zero,
        time_limit_sec=10,
        pattern_mode=False,
    )
    res_pen = solve_shift(
        children=[],
        staff=staff,
        requirements=table,
        settings=settings,
        weights=weights_penalty,
        time_limit_sec=10,
        pattern_mode=False,
    )

    assert res_zero.ok
    assert res_pen.ok
    # 週44時間を超えていないため、max_shift_length_penalty による目的関数の差は0であること
    assert abs(res_pen.objective_value - res_zero.objective_value) < 1e-5
