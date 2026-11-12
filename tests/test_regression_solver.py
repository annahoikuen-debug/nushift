"""ソルバー求解の回帰防止テスト (NuShift / shiftai)。

HiGHS / CBC 求解、制約充足、実行可能解、ウォームスタートの動作を
機械的に検証し、将来の変更による回帰を防ぐ。

docstring と違う「実態が assert 1 個だけ」の状態を正すため、
各テストは **解の品質そのもの**（ステータス・制約充足・配置充足率）を主張する。
"""

from __future__ import annotations

import datetime as dt

import pytest

from shiftai import local_rules
from shiftai.domain import (
    AgeClass,
    ChildPlan,
    Contract,
    EmploymentType,
    Role,
    StaffMember,
    StaffPreferences,
    SolveStatus,
)
from shiftai.gap_analysis import analyze_gap, check_violations
from shiftai.solver import solve_shift, verify_solution
from shiftai.standards import build_requirements

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


@pytest.fixture(scope="module")
def solved() -> tuple:
    """1 日・5 人の小さい問題を 1 回だけ解く（モジュール内で共有）。"""
    children = _children()
    staff = _staff()
    prefs = {s.staff_id: StaffPreferences() for s in staff}
    standard = _standard()
    table = build_requirements(
        children, [DAY], standard, day_open=DAY_OPEN, day_close=DAY_CLOSE, granularity_min=GRAN
    )
    result = solve_shift(children, staff, table, prefs, time_limit_sec=30, standard=standard)
    return children, staff, prefs, standard, table, result


def test_build_requirements_returns_table(solved) -> None:
    """build_requirements が有効な RequirementTable を返すこと。"""
    _, _, _, _, table, _ = solved
    assert table is not None
    assert table.slots
    assert table.for_day(DAY), "在園児がいるのに必要人員行が空"


def test_solve_shift_returns_result(solved) -> None:
    """solve_shift が実行可能解（OPTIMAL / FEASIBLE）を返すこと。

    5 人 vs 必要 1〜2 名という余裕のある問題で、
    「ステータスが設定された」だけでなく **解けた** ことを主張する。
    """
    _, _, _, _, _, result = solved
    assert result is not None
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE), (
        f"status={result.status}, messages={result.messages}"
    )
    assert result.ok is True


def test_solve_shift_covers_requirements(solved) -> None:
    """解いたシフトが配置基準を満たすこと（充足率 1.0・不足 0 件）。"""
    _, staff, _, standard, table, result = solved
    report = analyze_gap(table, result, staff, standard=standard)
    assert report.total_shortfall_slots == 0, (
        f"不足 {report.total_shortfall_slots} 時間帯: "
        f"{[g.to_dict() for g in report.gaps if g.is_shortfall]}"
    )
    assert report.coverage_ratio == pytest.approx(1.0)


def test_check_violations_returns_list(solved) -> None:
    """check_violations が違反リストを返すこと。"""
    _, staff, prefs, standard, table, result = solved
    violations = check_violations(table, result, staff, prefs, standard=standard)
    assert isinstance(violations, list)
    # 法令違反（BLOCKER）が無いこと
    blockers = [v for v in violations if v.severity.value == "法令違反"]
    assert not blockers, f"BLOCKER 違反: {[v.code for v in blockers]}"


def test_verify_solution_returns_list(solved) -> None:
    """verify_solution が検証メッセージのリストを返すこと。"""
    _, _, _, _, _, result = solved
    messages = verify_solution(result)  # type: ignore[arg-type]
    assert isinstance(messages, list)
    assert messages == [], f"ハード制約違反: {messages}"
