from datetime import date, time

from shiftai.domain import (
    SKILL_CLOSING,
    SKILL_OPENING,
    AgeClass,
    CellState,
    ChildPlan,
    ClassRoom,
    Contract,
    FacilitySettings,
    Role,
    StaffMember,
)
from shiftai.local_rules import get_standard
from shiftai.solver import solve_shift
from shiftai.standards import build_requirements


def test_opening_closing_skill_requirements():
    """開所・閉所に資格/スキルを持つ職員が割り振られることを検証。"""
    standard = get_standard("全国基準（厚労省）")
    day = date(2026, 11, 2)
    settings = FacilitySettings(
        day_open=time(7, 0),
        day_close=time(19, 0),
    )
    # 2 children across day
    children = [
        ChildPlan("C01", "園児1", day, AgeClass.INFANT, time(7, 0), time(19, 0)),
        ChildPlan("C02", "園児2", day, AgeClass.AGE_1, time(7, 0), time(19, 0)),
    ]
    reqs = build_requirements(
        children,
        days=[day],
        standard=standard,
        day_open=settings.day_open,
        day_close=settings.day_close,
    )

    # 3 staff:
    # S1 has opening skill
    # S2 has closing skill
    # S3 has no special skill
    contract = Contract(weekly_hours=40.0, daily_hours=8.0)
    staff = [
        StaffMember("S1", "早番リーダー", (Role.HOIKUSHI,), contract, skills=frozenset({SKILL_OPENING})),
        StaffMember("S2", "遅番担当", (Role.HOIKUSHI,), contract, skills=frozenset({SKILL_CLOSING})),
        StaffMember("S3", "一般保育士", (Role.HOIKUSHI,), contract, skills=frozenset()),
    ]

    res = solve_shift(
        children=children,
        staff=staff,
        requirements=reqs,
        settings=settings,
        standard=standard,
        require_opening_closing_skill=True,
        time_limit_sec=10,
    )
    assert res.ok, f"Solve failed: {res.messages}"

    # Verify first slot has S1 (who has opening skill)
    first_slot = reqs.slots[0]
    early_workers = [
        a.staff_id
        for a in res.assignments
        if a.day == day and a.slot == first_slot and a.state in (CellState.WORK, CellState.BREAK)
    ]
    assert "S1" in early_workers, f"Expected S1 with opening skill on first slot, got: {early_workers}"

    # Verify last slot has S2 (who has closing skill)
    last_slot = reqs.slots[-1]
    late_workers = [
        a.staff_id
        for a in res.assignments
        if a.day == day and a.slot == last_slot and a.state in (CellState.WORK, CellState.BREAK)
    ]
    assert "S2" in late_workers, f"Expected S2 with closing skill on last slot, got: {late_workers}"


def test_classroom_min_staff_assignment():
    """クラス担任がコア時間帯に自クラスへ割り振られることを検証。"""
    standard = get_standard("全国基準（厚労省）")
    day = date(2026, 11, 2)
    settings = FacilitySettings(
        day_open=time(8, 30),
        day_close=time(17, 30),
    )
    children = [
        ChildPlan("C01", "0歳児A", day, AgeClass.INFANT, time(9, 0), time(17, 0)),
    ]
    reqs = build_requirements(
        children,
        days=[day],
        standard=standard,
        day_open=settings.day_open,
        day_close=settings.day_close,
    )

    room_0 = ClassRoom("room_0", "ひよこ組", AgeClass.INFANT, capacity=6, min_staff=1)

    contract = Contract(weekly_hours=40.0, daily_hours=8.0)
    # S1 is assigned to room_0
    # S2 is floating
    staff = [
        StaffMember("S1", "0歳担任", (Role.HOIKUSHI,), contract, assigned_class_id="room_0"),
        StaffMember("S2", "フリー", (Role.HOIKUSHI,), contract, assigned_class_id=None),
    ]

    res = solve_shift(
        children=children,
        staff=staff,
        requirements=reqs,
        settings=settings,
        standard=standard,
        classrooms=[room_0],
        require_opening_closing_skill=True,
        time_limit_sec=10,
    )
    assert res.ok, f"Solve failed: {res.messages}"

    # Core slot (e.g. 10:00-10:30) must have S1
    core_slot = [s for s in reqs.slots if s.label == "10:00-10:30"][0]
    assigned_workers = [
        a.staff_id
        for a in res.assignments
        if a.day == day and a.slot == core_slot and a.state in (CellState.WORK, CellState.BREAK)
    ]
    assert "S1" in assigned_workers, f"S1 (room_0 assigned) must work core slot: {assigned_workers}"

