"""Tests for Phase 2 domain expansions and capacity-based demand generator."""

from __future__ import annotations

from datetime import date, time

from shiftai.demand import build_requirements_from_capacity, generate_standard_children
from shiftai.domain import (
    SKILL_LEADER,
    SKILL_OPENING,
    AgeClass,
    ClassCapacity,
    ClassRoom,
    Contract,
    EmploymentType,
    FacilityCapacity,
    Role,
    StaffMember,
)
from shiftai.local_rules import get_standard


def test_classroom_domain_model():
    """Verify ClassRoom creation and properties."""
    room = ClassRoom(
        class_id="CR0",
        name="ひよこ組",
        age_class=AgeClass.INFANT,
        capacity=6,
        min_staff=2,
        lead_staff_id="S01",
    )
    assert room.class_id == "CR0"
    assert room.age_class == AgeClass.INFANT
    assert room.capacity == 6
    assert room.lead_staff_id == "S01"


def test_staff_member_leadership_and_opening_skills():
    """Verify StaffMember leadership flags and opening/closing capability properties."""
    contract = Contract(weekly_hours=40.0, daily_hours=8.0, employment_type=EmploymentType.SEI)

    # 1. Staff with leader skill
    leader = StaffMember(
        staff_id="S01",
        name="主任保育士",
        roles=(Role.HOIKUSHI,),
        contract=contract,
        skills=frozenset({SKILL_LEADER}),
        assigned_class_id="CR0",
        can_lead=True,
    )
    assert leader.can_lead is True
    assert leader.can_open is True  # Leaders can open
    assert leader.can_close is True  # Leaders can close
    assert leader.assigned_class_id == "CR0"

    # 2. Staff with only opening skill
    opener = StaffMember(
        staff_id="S02",
        name="早番担当パート",
        roles=(Role.HOIKUSHI,),
        contract=contract,
        skills=frozenset({SKILL_OPENING}),
    )
    assert opener.can_lead is False
    assert opener.can_open is True
    assert opener.can_close is False


def test_demand_generator_from_capacity():
    """Verify requirement table generation directly from FacilityCapacity."""
    capacity = FacilityCapacity(
        classes=(
            ClassCapacity(
                age_class=AgeClass.INFANT,
                class_name="ひよこ組",
                total_capacity=6,
                standard_time_count=5,
                short_time_count=1,
            ),
            ClassCapacity(
                age_class=AgeClass.AGE_1,
                class_name="りす組",
                total_capacity=12,
                standard_time_count=10,
                short_time_count=2,
            ),
        )
    )

    assert capacity.total_capacity() == 18
    assert capacity.total_enrolled() == 18

    days = [date(2026, 10, 5), date(2026, 10, 6)]
    children = generate_standard_children(capacity, days)
    assert len(children) == 18 * 2  # 18 children across 2 days

    standard = get_standard("全国基準（厚労省）")
    req_table = build_requirements_from_capacity(
        capacity,
        days=days,
        standard=standard,
        day_open=time(7, 0),
        day_close=time(20, 0),
        granularity_min=30,
    )

    assert len(req_table.all_days()) == 2
    # Check that requirement rows exist for both days
    for day in days:
        day_reqs = req_table.for_day(day)
        assert len(day_reqs) > 0
        # Peak hours must require at least 4 staff (0yo 6/3=2, 1yo 12/6=2)
        peak_slot = [s for s in req_table.slots if s.label == "11:00-11:30"][0]
        needed_all = req_table.needed_staff(day, peak_slot)
        assert needed_all >= 4
