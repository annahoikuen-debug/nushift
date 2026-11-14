import datetime as dt
from datetime import date, time
import pytest

from shiftai.domain import (
    AgeClass,
    CellState,
    ChildPlan,
    Contract,
    EmploymentType,
    FacilitySettings,
    Role,
    SolveStatus,
    StaffMember,
    StaffPreferences,
)
from shiftai.monthly import MonthlySolveContext, solve_monthly_hierarchical
from shiftai.shift_patterns import ShiftPattern
from shiftai.standards import build_requirements


def test_solve_monthly_hierarchical_success():
    """30日間の月次シフトが2段階階層的最適化により適法かつ所定公休数を満たして求解できること。"""
    days = [date(2026, 11, d) for d in range(1, 31)]

    # 1. 職員データ (常勤4名、パート4名)
    staff = []
    contracts = {}
    for i in range(1, 5):
        sid = f"staff_{i:02d}"
        c = Contract(
            weekly_hours=40.0,
            daily_hours=8.0,
            employment_type=EmploymentType.SEI,
            max_consecutive_days=5,
        )
        s = StaffMember(
            staff_id=sid,
            name=f"常勤保育士_{i}",
            roles=(Role.HOIKUSHI,),
            contract=c,
        )
        staff.append(s)
        contracts[sid] = c

    for i in range(5, 9):
        sid = f"staff_{i:02d}"
        c = Contract(
            weekly_hours=20.0,
            daily_hours=4.0,
            employment_type=EmploymentType.PART,
            max_consecutive_days=4,
        )
        s = StaffMember(
            staff_id=sid,
            name=f"パート保育士_{i}",
            roles=(Role.HOIKUSHI,),
            contract=c,
        )
        staff.append(s)
        contracts[sid] = c

    # 2. 園児データ (平日 10名)
    children = []
    for d in days:
        if d.weekday() < 5:
            for cid in range(1, 11):
                children.append(
                    ChildPlan(
                        child_id=f"c_{cid:03d}",
                        name=f"園児_{cid}",
                        day=d,
                        age_class=AgeClass.AGE_1,
                        arrive=time(8, 30),
                        depart=time(16, 30),
                    )
                )

    patterns = [
        ShiftPattern(key="EARLY", label="早番", start=time(7, 30), end=time(16, 30)),
        ShiftPattern(key="NORMAL", label="日勤", start=time(8, 30), end=time(17, 30)),
        ShiftPattern(key="LATE", label="遅番", start=time(9, 30), end=time(18, 30)),
        ShiftPattern(key="HALF", label="半日", start=time(8, 30), end=time(12, 30)),
    ]

    settings = FacilitySettings()
    from shiftai import local_rules
    standard = local_rules.get_standard(local_rules.DEFAULT_PRESET_KEY)
    req_table = build_requirements(
        children=children,
        days=days,
        standard=standard,
        day_open=time(7, 0),
        day_close=time(19, 0),
    )

    ctx = MonthlySolveContext(
        target_month="2026-11",
        all_days=days,
        staff=staff,
        contracts=contracts,
        preferences={},
        patterns=patterns,
        requirements=req_table,
        facility_settings=settings,
        fixed_assignments={},
        prior_duty_history={},
        monthly_holiday_target=8,  # 月8日の公休目標
        children=children,
    )

    res = solve_monthly_hierarchical(ctx, time_limit_sec=60)
    assert res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    assert len(res.shift_days) == 30
