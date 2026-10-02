"""配置対象外の職員（園長・主任）の扱いに関する回帰テスト（T-04）。

``Role.ENJOGAKUIN`` は「園長・主任（配置対象外）」という名前を持つが、
かつてはソルバのどこからも参照されておらず、保育基準の人数を充足していた。
園長1名だけの園でも「基準を満たした」ように見えていた。
"""

from __future__ import annotations

from datetime import date, time

import pytest

from shiftai import gap_analysis, local_rules, solver
from shiftai.domain import (
    AgeClass,
    CellState,
    ChildPlan,
    Contract,
    EmploymentType,
    Role,
    StaffMember,
)
from shiftai.standards import build_requirements

STANDARD_KEY = "全国基準（厚労省）"
DAY = date(2026, 10, 5)
DAY_OPEN = time(9, 0)
DAY_CLOSE = time(15, 0)


def make_member(staff_id: str, *roles: Role) -> StaffMember:
    return StaffMember(
        staff_id,
        "",
        roles,
        Contract(weekly_hours=40.0, daily_hours=8.0, employment_type=EmploymentType.SEI),
    )


def make_table():
    """3 歳児 1 名の 1 日分。保育基準は 2 名/時間帯。"""
    children = [ChildPlan("C001", "", DAY, AgeClass.AGE_3, DAY_OPEN, DAY_CLOSE)]
    return build_requirements(
        children,
        [DAY],
        local_rules.get_standard(STANDARD_KEY),
        day_open=DAY_OPEN,
        day_close=DAY_CLOSE,
        granularity_min=30,
    )


def work_labels(result, staff_id: str, table) -> list[str]:
    """該当職員が勤務している時間帯のラベル。"""
    shift_day = result.shift_days[0] if result.shift_days else None
    if shift_day is None:
        return []
    return [slot.label for slot in table.slots if shift_day.get(staff_id, slot) is CellState.WORK]


# --- R1: is_placeable の判定 ------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        ((Role.HOIKUSHI,), True),
        ((Role.SHIENSHIIN,), True),
        ((Role.KANGSHI,), True),
        ((Role.YOUCHUIN,), True),
        ((Role.ENJOGAKUIN,), False),
        ((Role.ENJOGAKUIN, Role.KANGSHI), False),
        # 副資格に保育士があれば保育室の補助ができるため配置可とする
        ((Role.ENJOGAKUIN, Role.HOIKUSHI), True),
    ],
)
def test_is_placeableの判定(roles: tuple[Role, ...], expected: bool) -> None:
    assert make_member("S001", *roles).is_placeable is expected


# --- R2 / R3: 園長は勤務を割り当てない ---------------------------------------


def test_園長は勤務セルを持たない() -> None:
    """園長1名だけの場合、勤務セルが1つも割り当てられないこと。"""
    table = make_table()
    encho = [make_member("E001", Role.ENJOGAKUIN)]

    result = solver.solve_shift(
        [], encho, table, time_limit_sec=15, standard=local_rules.get_standard(STANDARD_KEY)
    )

    assert work_labels(result, "E001", table) == []


def test_園長だけでは配置基準を満たせない() -> None:
    """園長1名だけの園では不足として検出されること。"""
    table = make_table()
    encho = [make_member("E001", Role.ENJOGAKUIN)]

    result = solver.solve_shift(
        [], encho, table, time_limit_sec=15, standard=local_rules.get_standard(STANDARD_KEY)
    )
    report = gap_analysis.analyze_gap(
        table, result, encho, standard=local_rules.get_standard(STANDARD_KEY)
    )

    shortfalls = [g for g in report.gaps if g.is_shortfall]
    assert shortfalls, "配置基準を満たせないこと"
    assert all(g.actual_staff == 0 for g in shortfalls)


def test_園長だけの園は要員不足を明示する() -> None:
    """誤差や不本意な部分解ではなく、供給が 0 であることを示すこと。"""
    table = make_table()
    encho = [make_member("E001", Role.ENJOGAKUIN)]

    result = solver.solve_shift(
        [], encho, table, time_limit_sec=15, standard=local_rules.get_standard(STANDARD_KEY)
    )

    assert result.stats.get("num_staff") == 0
    joined = "\n".join(result.messages)
    assert "供給0名" in joined or "0名配置可能" in joined, joined


# --- R4: 混在時に園長を数えない ---------------------------------------------


def test_混在時に園長は必要人員に数えられない() -> None:
    """保育士1名と園長1名で必要2名なら、1名不足になること。"""
    table = make_table()
    mixed = [make_member("S001", Role.HOIKUSHI), make_member("E001", Role.ENJOGAKUIN)]

    result = solver.solve_shift(
        [], mixed, table, time_limit_sec=15, standard=local_rules.get_standard(STANDARD_KEY)
    )
    report = gap_analysis.analyze_gap(
        table, result, mixed, standard=local_rules.get_standard(STANDARD_KEY)
    )

    assert work_labels(result, "E001", table) == []
    assert work_labels(result, "S001", table), "保育士には勤務が割り当てられること"
    shortfalls = [g for g in report.gaps if g.is_shortfall]
    assert shortfalls, "園長を数えないため 1 名不足になること"
    assert shortfalls[0].actual_staff == 1
    assert shortfalls[0].needed_staff == 2


# --- R5 / R7: 供給量と過不足集計 ---------------------------------------------


def test_供給人数に園長を数えない() -> None:
    """``supply_hours`` は園長の人時を加算しないこと。"""
    table = make_table()
    encho = [make_member("E001", Role.ENJOGAKUIN)]

    assert solver.supply_hours(encho, table, {}, None) == 0.0


def test_供給人数に副資格の保育士は数える() -> None:
    """副資格に保育士がある園長は供給に含めてよいこと。"""
    table = make_table()
    both = [make_member("B001", Role.ENJOGAKUIN, Role.HOIKUSHI)]

    assert solver.supply_hours(both, table, {}, None) > 0.0


def test_過不足集計に園長を数えない() -> None:
    """``analyze_gap`` の配置人数に園長が含まれないこと。"""
    table = make_table()
    mixed = [make_member("S001", Role.HOIKUSHI), make_member("E001", Role.ENJOGAKUIN)]

    result = solver.solve_shift(
        [], mixed, table, time_limit_sec=15, standard=local_rules.get_standard(STANDARD_KEY)
    )
    report = gap_analysis.analyze_gap(
        table, result, mixed, standard=local_rules.get_standard(STANDARD_KEY)
    )

    assert report.gaps
    assert all(g.actual_staff <= 1 for g in report.gaps), "配置人数は保育士1名までであること"


# --- 貪欲法も同じ扱い -------------------------------------------------------


def test_貪欲法も園長を配置しない() -> None:
    """MILP が使えないときの貪欲法でも園長を勤務に充てないこと。"""
    table = make_table()
    mixed = [make_member("S001", Role.HOIKUSHI), make_member("E001", Role.ENJOGAKUIN)]

    result = solver.solve_shift_greedy(
        [], mixed, table, standard=local_rules.get_standard(STANDARD_KEY)
    )

    assert work_labels(result, "E001", table) == []
