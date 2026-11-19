"""Timefold 思想に基づく SwapMove / ChangeMove / LAHC の単体テスト。"""

from __future__ import annotations

from datetime import date, time

from shiftai.domain import (
    AgeClass,
    CellState,
    Contract,
    Requirement,
    RequirementTable,
    Role,
    ShiftDay,
    Slot,
    SolveResult,
    SolveStatus,
    StaffMember,
    StaffPreferences,
    Unavailability,
)
from shiftai.local_search import ruin_and_recreate
from shiftai.shift_patterns import ShiftPattern


def test_local_search_swap_move_resolves_conflict():
    """SwapMove により、2名のパターンを交換して希望休違反が解消されることを検証。"""
    d = date(2026, 4, 1)
    slot1 = Slot(start=time(7, 0), end=time(11, 0))  # 早番スロット
    slot2 = Slot(start=time(15, 0), end=time(19, 0))  # 遅番スロット
    slots = (slot1, slot2)

    s1 = StaffMember(
        staff_id="S001",
        name="職員A",
        roles=(Role.HOIKUSHI,),
        contract=Contract(weekly_hours=40.0, daily_hours=8.0),
    )
    s2 = StaffMember(
        staff_id="S002",
        name="職員B",
        roles=(Role.HOIKUSHI,),
        contract=Contract(weekly_hours=40.0, daily_hours=8.0),
    )
    staff = [s1, s2]

    # 初期割り当て: S001 が早番(slot1)、S002 が遅番(slot2)
    # しかし S001 は早番NG (slot1 が希望休)
    initial_sd = ShiftDay(
        day=d,
        assignments={
            "S001": {slot1.label: CellState.WORK, slot2.label: CellState.OFF},
            "S002": {slot1.label: CellState.OFF, slot2.label: CellState.WORK},
        },
    )

    prefs = {
        "S001": StaffPreferences(
            unavailable=[Unavailability(day=d, start=time(7, 0), end=time(11, 0))],
        ),
        "S002": StaffPreferences(),
    }

    req_table = RequirementTable(
        day_open=time(7, 0),
        day_close=time(19, 0),
        granularity_min=240,
        slots=slots,
        rows={
            d: [
                Requirement(
                    day=d,
                    slot=slot1,
                    age_class=AgeClass.AGE_3,
                    child_count=10,
                    needed_staff=1,
                    needed_qualified=1,
                ),
                Requirement(
                    day=d,
                    slot=slot2,
                    age_class=AgeClass.AGE_3,
                    child_count=10,
                    needed_staff=1,
                    needed_qualified=1,
                ),
            ]
        },
    )

    init_res = SolveResult(
        status=SolveStatus.FEASIBLE,
        shift_days=[initial_sd],
        violations=[],
    )

    pat1 = ShiftPattern(key="early", label="早番", symbol="早", start=time(7, 0), end=time(11, 0))
    pat2 = ShiftPattern(key="late", label="遅番", symbol="遅", start=time(15, 0), end=time(19, 0))
    result = ruin_and_recreate(
        initial_result=init_res,
        requirements=req_table,
        staff=staff,
        preferences=prefs,
        patterns=[pat1, pat2],
        max_iterations=10,
        lahc_size=5,
    )

    assert result.ok
    assert result.stats.get("local_search_improved") is True
    fin_sd = result.shift_days[0]
    assert fin_sd.assignments["S001"][slot1.label] == CellState.OFF
    assert fin_sd.assignments["S001"][slot2.label] == CellState.WORK
    assert fin_sd.assignments["S002"][slot1.label] == CellState.WORK
    assert fin_sd.assignments["S002"][slot2.label] == CellState.OFF


def test_local_search_change_move():
    """ChangeMove により出勤者のパターンを変更して希望休が解消されることを検証。"""
    d = date(2026, 4, 1)
    slot1 = Slot(start=time(7, 0), end=time(11, 0))
    slot2 = Slot(start=time(11, 0), end=time(15, 0))
    slots = (slot1, slot2)

    s1 = StaffMember(
        staff_id="S001",
        name="職員A",
        roles=(Role.HOIKUSHI,),
        contract=Contract(weekly_hours=40.0, daily_hours=8.0),
    )
    staff = [s1]

    # 初期割り当て: slot1 に出勤しているが slot1 は希望休
    initial_sd = ShiftDay(
        day=d,
        assignments={
            "S001": {slot1.label: CellState.WORK, slot2.label: CellState.OFF},
        },
    )

    prefs = {
        "S001": StaffPreferences(
            unavailable=[Unavailability(day=d, start=time(7, 0), end=time(11, 0))],
        ),
    }

    req_table = RequirementTable(
        day_open=time(7, 0),
        day_close=time(15, 0),
        granularity_min=240,
        slots=slots,
        rows={
            d: [
                Requirement(
                    day=d,
                    slot=slot1,
                    age_class=AgeClass.AGE_3,
                    child_count=0,
                    needed_staff=0,
                    needed_qualified=0,
                ),
                Requirement(
                    day=d,
                    slot=slot2,
                    age_class=AgeClass.AGE_3,
                    child_count=10,
                    needed_staff=1,
                    needed_qualified=1,
                ),
            ]
        },
    )

    init_res = SolveResult(
        status=SolveStatus.FEASIBLE,
        shift_days=[initial_sd],
        violations=[],
    )

    pat_day = ShiftPattern(key="day", label="日勤", symbol="日", start=time(11, 0), end=time(15, 0))
    result = ruin_and_recreate(
        initial_result=init_res,
        requirements=req_table,
        staff=staff,
        preferences=prefs,
        patterns=[pat_day],
        max_iterations=10,
        lahc_size=5,
    )

    assert result.ok
    assert result.stats.get("local_search_improved") is True
    fin_sd = result.shift_days[0]
    assert fin_sd.assignments["S001"][slot1.label] == CellState.OFF
    assert fin_sd.assignments["S001"][slot2.label] == CellState.WORK
