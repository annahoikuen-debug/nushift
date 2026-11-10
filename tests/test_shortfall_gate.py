"""求解前ショートフォールゲートの単体テスト。"""

from __future__ import annotations

from datetime import date, time

from shiftai.domain import (
    AgeClass,
    Contract,
    EmploymentType,
    FacilitySettings,
    Requirement,
    RequirementTable,
    Role,
    Slot,
    SlotKind,
    StaffMember,
)
from shiftai.local_rules import get_standard
from shiftai.ui.tab_solve import shortfall_gate


def test_shortfall_gate_with_loose_requirements():
    """需要が十分に少なく供給が足りている場合は空リストが返ること。"""
    standard = get_standard("全国基準（厚労省）")
    day = date(2026, 11, 2)
    slot = Slot(start=time(9, 0), end=time(17, 0))
    table = RequirementTable(
        day_open=time(9, 0),
        day_close=time(17, 0),
        granularity_min=480,
        slots=(slot,),
        rows={
            day: [
                Requirement(
                    day=day,
                    slot=slot,
                    age_class=AgeClass.AGE_3,
                    child_count=5,
                    needed_staff=1,
                    needed_qualified=1,
                    slot_kind=SlotKind.NORMAL,
                )
            ]
        },
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
        day_close=time(17, 0),
        granularity_min=480,
        closed_days=frozenset(),
        holiday_dates=frozenset(),
    )

    gaps = shortfall_gate(table, staff, preferences={}, settings=settings, standard=standard)
    assert gaps == []


def test_shortfall_gate_with_tight_requirements():
    """需要が供給を大きく上回る場合は不足行が返ること。"""
    standard = get_standard("全国基準（厚労省）")
    day = date(2026, 11, 2)
    slot = Slot(start=time(9, 0), end=time(17, 0))
    # need=99 で実現不可能な需要
    table = RequirementTable(
        day_open=time(9, 0),
        day_close=time(17, 0),
        granularity_min=480,
        slots=(slot,),
        rows={
            day: [
                Requirement(
                    day=day,
                    slot=slot,
                    age_class=AgeClass.AGE_3,
                    child_count=100,
                    needed_staff=99,
                    needed_qualified=99,
                    slot_kind=SlotKind.NORMAL,
                )
            ]
        },
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
    ]
    settings = FacilitySettings(
        day_open=time(9, 0),
        day_close=time(17, 0),
        granularity_min=480,
        closed_days=frozenset(),
        holiday_dates=frozenset(),
    )

    gaps = shortfall_gate(table, staff, preferences={}, settings=settings, standard=standard)
    assert len(gaps) > 0
