"""局所探索 (Ruin & Recreate) の単体テスト。"""

import datetime as dt
from datetime import date, time

from shiftai import local_rules
from shiftai.domain import (
    AgeClass,
    CellState,
    ChildPlan,
    Contract,
    EmploymentType,
    FacilitySettings,
    Role,
    ShiftDay,
    SolveResult,
    SolveStatus,
    StaffMember,
)
from shiftai.local_search import ruin_and_recreate
from shiftai.standards import build_requirements


def test_ruin_and_recreate_repairs_coverage():
    """不足スロットのある破綻初期解に対し、局所探索が代替職員を再割当してスコア改善すること。"""
    days = [date(2026, 11, d) for d in range(1, 8)]

    staff = [
        StaffMember(
            staff_id="s1",
            name="正規1",
            roles=[Role.HOIKUSHI],
            contract=Contract(weekly_hours=40.0, daily_hours=8.0, employment_type=EmploymentType.SEI),
        ),
        StaffMember(
            staff_id="s2",
            name="正規2",
            roles=[Role.HOIKUSHI],
            contract=Contract(weekly_hours=40.0, daily_hours=8.0, employment_type=EmploymentType.SEI),
        ),
    ]

    children = [
        ChildPlan(child_id="c1", name="園児1", day=d, age_class=AgeClass.AGE_1, arrive=time(9, 0), depart=time(17, 0))
        for d in days
    ]

    standard = local_rules.get_standard(local_rules.DEFAULT_PRESET_KEY)
    reqs = build_requirements(children=children, days=days, standard=standard, day_open=time(9, 0), day_close=time(17, 0))

    # 全員 OFF の破綻解
    bad_days = [
        ShiftDay(day=d, assignments={
            "s1": {s.label: CellState.OFF for s in reqs.slots},
            "s2": {s.label: CellState.OFF for s in reqs.slots},
        })
        for d in days
    ]
    initial_res = SolveResult(status=SolveStatus.PARTIAL, shift_days=bad_days, violations=[])

    improved_res = ruin_and_recreate(
        initial_result=initial_res,
        requirements=reqs,
        staff=staff,
        standard=standard,
        settings=FacilitySettings(),
    )

    # 局所探索により改善されていること
    assert improved_res.stats["local_search_improved"] is True
    # 最低限誰かが WORK に割り当てられていること
    worked_count = sum(
        1 for sd in improved_res.shift_days
        for sid, s_dict in sd.assignments.items()
        for state in s_dict.values() if state == CellState.WORK
    )
    assert worked_count > 0
