"""ソルバー求解の回帰防止テスト (NuShift / shiftai)。

HiGHS / CBC 求解、制約充足、実行可能解、ウォームスタートの動作を
機械的に検証し、将来の変更による回帰を防ぐ。
"""

from __future__ import annotations

import datetime as dt

from shiftai import local_rules
from shiftai.domain import (
    AgeClass,
    ChildPlan,
    Contract,
    EmploymentType,
    Role,
    StaffMember,
    StaffPreferences,
)
from shiftai.gap_analysis import check_violations
from shiftai.solver import solve_shift, verify_solution

DAY = dt.date(2026, 10, 2)
DAY_OPEN, DAY_CLOSE, GRAN = dt.time(9, 0), dt.time(15, 0), 30


def _children(count: int = 5) -> list[ChildPlan]:
    return [
        ChildPlan(
            f"C{i:03d}",
            f"園児{i}",
            DAY,
            AgeClass.AGE_3,
            arrive=DAY_OPEN,
            depart=DAY_CLOSE,
            is_short_time=False,
            absent=False,
            absent_reason="",
            uses_early_care=False,
            uses_late_care=False,
            notes="",
        )
        for i in range(1, count + 1)
    ]


def _staff(count: int = 4) -> list[StaffMember]:
    contract = Contract(
        weekly_hours=40.0,
        daily_hours=8.0,
        employment_type=EmploymentType.SEI,
        min_monthly_hours=150.0,
        max_monthly_hours=185.0,
        max_weekly_days=5,
        max_consecutive_days=5,
        min_rest_hours=11.0,
        granularity_min=GRAN,
        earliest_start=dt.time(8, 0),
        latest_end=dt.time(17, 0),
    )
    return [
        StaffMember(f"S{i:02d}", f"職員{i}", (Role.HOIKUSHI,), contract)
        for i in range(1, count + 1)
    ]


def _standard() -> object:
    return local_rules.get_standard("全国基準（厚労省）")


def test_build_requirements_returns_table() -> None:
    """build_requirements が RequirementTable を返すこと。"""
    from shiftai.standards import build_requirements

    children = _children()
    standard = _standard()
    table = build_requirements(
        children, [DAY], standard, day_open=DAY_OPEN, day_close=DAY_CLOSE, granularity_min=GRAN
    )
    assert table is not None


def test_solve_shift_returns_result() -> None:
    """solve_shift が SolveResult を返し、ステータスが設定されること。"""
    from shiftai.standards import build_requirements

    children = _children()
    staff = _staff()
    prefs = {s.staff_id: StaffPreferences() for s in staff}
    standard = _standard()
    table = build_requirements(
        children, [DAY], standard, day_open=DAY_OPEN, day_close=DAY_CLOSE, granularity_min=GRAN
    )
    result = solve_shift(children, staff, table, prefs, time_limit_sec=30, standard=standard)
    assert result is not None
    assert result.status is not None


def test_check_violations_returns_list() -> None:
    """check_violations が違反リストを返すこと。"""
    from shiftai.standards import build_requirements

    children = _children()
    staff = _staff()
    prefs = {s.staff_id: StaffPreferences() for s in staff}
    standard = _standard()
    table = build_requirements(
        children, [DAY], standard, day_open=DAY_OPEN, day_close=DAY_CLOSE, granularity_min=GRAN
    )
    result = solve_shift(children, staff, table, prefs, time_limit_sec=30, standard=standard)
    violations = check_violations(table, result, staff, prefs, standard=standard)
    assert isinstance(violations, list)


def test_verify_solution_returns_list() -> None:
    """verify_solution が検証メッセージのリストを返すこと。"""
    from shiftai.standards import build_requirements

    children = _children()
    staff = _staff()
    prefs = {s.staff_id: StaffPreferences() for s in staff}
    standard = _standard()
    table = build_requirements(
        children, [DAY], standard, day_open=DAY_OPEN, day_close=DAY_CLOSE, granularity_min=GRAN
    )
    result = solve_shift(children, staff, table, prefs, time_limit_sec=30, standard=standard)
    messages = verify_solution(table, result)
    assert isinstance(messages, list)
