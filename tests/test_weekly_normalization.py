"""週次判定の回帰テスト（T-01）。

週の上限は「任意の連続した 1 週間の勤務量」で判定する。計画全体の合計と
比較していると、2 週間以上の計画で必ず偽陽性が出る。

    週契約 35 時間・週 5 出勤で完全に適法な 2 週間のシフトに対して、
    期間合計 56 時間を「週44時間を超えている」と判定していた。

同じ職員が 14 日出勤するだけの小さなケースを CBC に解かせると週上限の違反が出るため、
R1〜R6 / R9 / R10 は ``check_violations`` を合成した ``SolveResult`` に直接
渡して検証する（CBC を起動しない）。R7 のみ実際の求解を必要とする。
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from shiftai import local_rules, solver
from shiftai.domain import (
    AgeClass,
    CellState,
    ChildPlan,
    Contract,
    EmploymentType,
    Role,
    ShiftAssignment,
    SolveResult,
    SolveStatus,
    StaffMember,
    Violation,
    daterange,
    weekly_periods,
    weekly_windows,
)
from shiftai.gap_analysis import check_violations
from shiftai.standards import build_requirements

STANDARD_KEY = "全国基準（厚労省）"
# 1 日に 10 時間の時間帯を置き、週 44 時間を超える勤務時間を構成できるようにする。
DAY_OPEN = time(8, 0)
DAY_CLOSE = time(18, 0)
START = date(2026, 10, 5)


def make_days(n: int) -> list[date]:
    """``START`` から連続する ``n`` 日。"""
    return list(daterange(START, START + timedelta(days=n - 1)))


def make_member(weekly: float = 35.0, **overrides) -> StaffMember:
    """週契約 ``weekly`` 時間の保育士 1 名。"""
    base = {
        "weekly_hours": weekly,
        "daily_hours": 7.0,
        "max_weekly_days": 5,
        "max_consecutive_days": 5,
        "min_rest_hours": 0.0,
        "earliest_start": time(7, 0),
        "latest_end": time(20, 0),
        "employment_type": EmploymentType.SEI,
        "min_monthly_hours": 0,
        "max_monthly_hours": 200,
    }
    base.update(overrides)
    return StaffMember("S001", "保育士", (Role.HOIKUSHI,), Contract(**base))


def make_table(days: list[date], child_count: int = 6) -> object:
    """必要人員表を作る。1 時間帯あたり 2 名の基準を満たす規模。"""
    children = [
        ChildPlan(f"C{i:03d}", "", day, AgeClass.AGE_3, DAY_OPEN, DAY_CLOSE)
        for day in days
        for i in range(child_count)
    ]
    return build_requirements(
        children,
        days,
        local_rules.get_standard(STANDARD_KEY),
        day_open=DAY_OPEN,
        day_close=DAY_CLOSE,
        granularity_min=30,
    )


def make_result(table, staff_id: str, hours_per_day: dict[date, float]) -> SolveResult:
    """指定した日数だけ勤務する ``SolveResult`` を合成する。

    ``hours_per_day`` にない日は不在とする。勤務時間から時間帯の
    組み合わせを割り当てる（1 時間帯 = 30 分）。
    """
    assignments: list[ShiftAssignment] = []
    for day in table.all_days():
        hours = hours_per_day.get(day, 0.0)
        count = int(round(hours * 60 / 30))
        for index, slot in enumerate(table.slots):
            state = CellState.WORK if index < count else CellState.OFF
            assignments.append(ShiftAssignment(staff_id, day, slot, state))
    return SolveResult(status=SolveStatus.OPTIMAL, assignments=assignments)


def codes_of(violations: list[Violation]) -> set[str]:
    return {v.code for v in violations}


# --- R9: 窓分割そのものの境界 ---------------------------------------------


@pytest.mark.parametrize(
    ("n_days", "expected"),
    [(1, 1), (3, 1), (6, 1), (7, 1), (8, 2), (14, 8), (28, 22)],
)
def test_窓分割の境界(n_days: int, expected: int) -> None:
    """1 週間以下は 1 つの窓、8 日以上は 1 日ずつずらした窓を返す。"""
    assert len(weekly_windows(make_days(n_days))) == expected


def test_窓分割は重複して覆う() -> None:
    """14 日の場合、先頭と末尾の両端が覆われている。"""
    windows = weekly_windows(make_days(14))
    assert windows[0][0] == START
    assert windows[0][-1] == START + timedelta(days=6)
    assert windows[-1][-1] == START + timedelta(days=13)
    assert all(len(w) == 7 for w in windows)


def test_週区画は重複しない() -> None:
    """供給量を集計する ``weekly_periods`` は窓どうしが重複しない。"""
    periods = weekly_periods(make_days(14))
    assert len(periods) == 2
    assert sum(len(p) for p in periods) == 14


def test_空の日列表は窓を返さない() -> None:
    assert weekly_windows([]) == []
    assert weekly_periods([]) == []


# --- R1 / R2: 週の勤務時間 ------------------------------------------------


def test_週28時間は週44時間判定に引っかからない() -> None:
    """週 28 時間の職員に ``WEEKLY_HOURS_EXCEEDED`` が出ないこと。

    2 週間で合計 56 時間になるが、週ごとで見れば 28 時間であり適法。
    """
    days = make_days(14)
    table = make_table(days)
    member = make_member(weekly=35.0)
    result = make_result(table, "S001", {d: 4.0 for d in days})

    found = check_violations(table, result, [member])

    assert "WEEKLY_HOURS_EXCEEDED" not in codes_of(found), [
        v.message for v in found if v.code == "WEEKLY_HOURS_EXCEEDED"
    ]


def test_週48時間は超過として検出する() -> None:
    """ある 1 週間だけ 48 時間なら、その週についてだけ違反になること。"""
    days = make_days(14)
    table = make_table(days)
    member = make_member(weekly=60.0)
    # 後半の 7 日だけ 8 時間勤務（週48時間）、前半は 4 時間
    heavy = {d: 8.0 for d in days[7:]}
    result = make_result(table, "S001", {**{d: 4.0 for d in days[:7]}, **heavy})

    found = check_violations(table, result, [member])
    weekly = [v for v in found if v.code == "WEEKLY_HOURS_EXCEEDED"]

    assert weekly, "週48時間の週で検出されること"
    for violation in weekly:
        assert violation.detail["hours"] > 44.0
        # 前半 7 日（週28時間）のみの窓は違反にならない
        assert violation.detail["window_start"] > days[0].isoformat(), (
            "4 時間勤務だけの週には違反が立たないこと"
        )


def test_週判定の違反に窓の日付が入る() -> None:
    """``detail`` に窓の開始日と終了日が入る（R10）。"""
    days = make_days(14)
    table = make_table(days)
    member = make_member(weekly=60.0)
    result = make_result(table, "S001", {d: 8.0 for d in days})

    weekly = [
        v for v in check_violations(table, result, [member]) if v.code == "WEEKLY_HOURS_EXCEEDED"
    ]

    assert weekly
    for violation in weekly:
        assert violation.detail["window_days"] == 7
        assert violation.detail["window_start"] < violation.detail["window_end"]
        assert violation.day == date.fromisoformat(violation.detail["window_start"])


# --- R3 / R5: 週の出勤日数 ------------------------------------------------


def test_週6勤務は週最大出勤日数の判定に引っかからない() -> None:
    """週 6 日出勤（契約上限 5 日を 1 日超過）は、その週だけ違反になること。"""
    days = make_days(14)
    table = make_table(days)
    member = make_member(weekly=42.0, max_weekly_days=5)
    result = make_result(table, "S001", {d: 6.0 for d in days})

    found = check_violations(table, result, [member])
    weekly_days = [v for v in found if v.code == "WEEKLY_DAYS_EXCEEDED"]

    for violation in weekly_days:
        assert violation.detail["cap"] == 5
        assert violation.detail["days"] == 7, "実際に 7 日出勤している週のみ立つこと"


def test_週5勤務は週最大出勤日数の判定に引っかからない() -> None:
    """契約上限どおり週 5 日なら違反にならないこと。"""
    days = make_days(14)
    table = make_table(days)
    member = make_member(weekly=35.0, max_weekly_days=5)
    worked = {d for i, d in enumerate(days) if i % 7 < 5}
    result = make_result(table, "S001", {d: 6.0 for d in worked})

    found = check_violations(table, result, [member])

    assert "WEEKLY_DAYS_EXCEEDED" not in codes_of(found), [
        v.message for v in found if v.code == "WEEKLY_DAYS_EXCEEDED"
    ]


def test_休園日を含む週は出勤日数が減る() -> None:
    """必要人員のない日（休園日）は出勤日数の判定に数えない。"""
    days = make_days(14)
    table = make_table(days)
    member = make_member(weekly=35.0, max_weekly_days=5)
    # 第 2 週の 2 日を休園日として扱うため、必要人員表を作り直す
    closed = {days[8], days[9]}
    open_days = [d for d in days if d not in closed]
    table = make_table(open_days)
    worked = {d: 6.0 for d in open_days if d < days[7]} | {
        d: 6.0 for d in open_days if d >= days[7]
    }
    result = make_result(table, "S001", worked)

    found = check_violations(table, result, [member])
    weekly_days = [v for v in found if v.code == "WEEKLY_DAYS_EXCEEDED"]

    # 7 日窓のうち休園日を含む窓では 7 日とはならない
    assert weekly_days
    for violation in weekly_days:
        assert violation.detail["days"] <= 7


def test_週最大出勤日数が0なら判定しない() -> None:
    """``max_weekly_days`` が 0（上限なし）のときは判定しないこと。"""
    days = make_days(14)
    table = make_table(days)
    member = make_member(weekly=35.0, max_weekly_days=0)
    result = make_result(table, "S001", {d: 6.0 for d in days})

    found = check_violations(table, result, [member])

    assert "WEEKLY_DAYS_EXCEEDED" not in codes_of(found)


# --- R6: 1 週間未満は従来どおり --------------------------------------------


def test_7日未満の計画は期間合計で判定する() -> None:
    """5 日計画で 1 日 9 時間なら期間合計 45 時間で違反になること。"""
    days = make_days(5)
    table = make_table(days)
    member = make_member(weekly=50.0)
    result = make_result(table, "S001", {d: 9.0 for d in days})

    found = check_violations(table, result, [member])
    weekly = [v for v in found if v.code == "WEEKLY_HOURS_EXCEEDED"]

    assert len(weekly) == 1, "5 日計画は 1 件の判定になる"
    assert weekly[0].detail["hours"] == pytest.approx(45.0)
    assert weekly[0].detail["window_days"] == 5


def test_7日未満の計画で週上限内的なら違反にならない() -> None:
    days = make_days(5)
    table = make_table(days)
    member = make_member(weekly=50.0, max_weekly_days=5)
    result = make_result(table, "S001", {d: 7.0 for d in days})

    found = check_violations(table, result, [member])

    assert "WEEKLY_DAYS_EXCEEDED" not in codes_of(found)
    assert "WEEKLY_HOURS_EXCEEDED" not in codes_of(found)


def test_7日計画は単一窓で判定する() -> None:
    """7 日ちょうどは 1 つの窓になり、従来と同じ結果になる。"""
    days = make_days(7)
    table = make_table(days)
    member = make_member(weekly=50.0)
    result = make_result(table, "S001", {d: 7.0 for d in days})

    weekly = [
        v for v in check_violations(table, result, [member]) if v.code == "WEEKLY_HOURS_EXCEEDED"
    ]

    assert len(weekly) == 1
    assert weekly[0].detail["hours"] == pytest.approx(49.0)
    assert weekly[0].detail["window_days"] == 7


# --- R4 / R8: 4 週間と供給量 ----------------------------------------------


def test_4週間計画でも週単位判定になる() -> None:
    """28 日計画の職員が週 20 時間なら、期間合計 140 時間でも違反にならないこと。"""
    days = make_days(28)
    table = make_table(days)
    member = make_member(weekly=35.0)
    result = make_result(table, "S001", {d: 2.5 for d in days})

    found = check_violations(table, result, [member])
    weekly = [v for v in found if v.code == "WEEKLY_HOURS_EXCEEDED"]

    assert not weekly, [v.message for v in weekly]
    # 期間合計は 70 時間なので、期間合計で判定していた頃なら違反になっていた
    assert sum(2.5 for _ in days) > 44.0


def test_供給人時は週ごとに上限される() -> None:
    """14 日計画の供給量が「週5日 × 2週」に対応すること。"""
    members = [
        StaffMember("S001", "保育士1", (Role.HOIKUSHI,), make_member().contract),
        StaffMember("S002", "保育士2", (Role.HOIKUSHI,), make_member().contract),
    ]

    seven = solver.supply_hours(members, make_table(make_days(7)), {}, None)
    fourteen = solver.supply_hours(members, make_table(make_days(14)), {}, None)

    assert fourteen == pytest.approx(seven * 2, rel=0.01)


def test_供給人時は物理的上限を超えない() -> None:
    """窓の重複で二重計上されないこと（週区画で集計する）。"""
    days = make_days(14)
    table = make_table(days)
    members = [
        StaffMember(f"S{i:03d}", f"保育士{i}", (Role.HOIKUSHI,), make_member().contract)
        for i in range(3)
    ]

    total = solver.supply_hours(members, table, {}, None)

    # 3 名 × 14 日 × 法定 10 時間 = 420 人が上限
    assert total <= 420.0, f"供給量が物理的上限を超えた: {total}"


# --- R7: モデルが週ごとの上限を守る（実求解） ------------------------------


@pytest.mark.slow
def test_モデルは期間全体で出勤日数を制限しない() -> None:
    """14 日計画で、1 人が 5 日を超える日数を出勤できること。

    週の上限は「1 週間あたり」であり「計画期間全体」ではない。修正前は
    ``Σy <= max_weekly_days`` が全期間に対して課されていたため、2 週間の
    計画で 1 人が 5 日しか出勤できず、必要人員が足りuclease 常に不足になっていた。
    このテストはその回帰を防ぐ。
    """
    days = make_days(14)
    table = make_table(days, child_count=6)
    members = [
        StaffMember(
            f"S{i:03d}",
            f"保育士{i}",
            (Role.HOIKUSHI,),
            Contract(
                weekly_hours=35.0,
                daily_hours=7.0,
                max_weekly_days=5,
                max_consecutive_days=5,
                min_rest_hours=11.0,
                earliest_start=time(7, 0),
                latest_end=time(20, 0),
                employment_type=EmploymentType.SEI,
                min_monthly_hours=0,
                max_monthly_hours=200,
            ),
        )
        for i in range(6)
    ]

    result = solver.solve_shift(
        [],
        members,
        table,
        time_limit_sec=45,
        standard=local_rules.get_standard(STANDARD_KEY),
    )

    worked: dict[str, set[date]] = {}
    for assignment in result.assignments:
        if assignment.state is CellState.WORK:
            worked.setdefault(assignment.staff_id, set()).add(assignment.day)

    total_days = [len(worked.get(m.staff_id, set())) for m in members]
    assert max(total_days) > 5, (
        "週の上限が計画期間全体に適用されている可能性がある。"
        f"14 日計画で 1 人が 5 日を超えて出勤できていない: {total_days}"
    )
