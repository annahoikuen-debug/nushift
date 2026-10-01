"""人件費の計算に関する回帰テスト（T-05）。

人件費は UI 指標（``gap_analysis.compute_cost`` / ``summarize``）と給与 CSV
（``exporter.payroll_dataframe``）の両方に出るため、式と係数がずれると
「画面と CSV が食い違う」という不具合になる。修正前は次の3つが重なっていた。

* ``summarize()`` が ``settings`` を受け取らず、単価が常に既定の 1500 円だった
* ``compute_cost`` は在勤基準（勤務 + 休憩）、``payroll_dataframe`` は実働基準だった
* ``payroll_dataframe`` は ``worked_min``（休憩を含まない値）からさらに
  ``break_min`` を引いており、休憩時間が二重控除されていた
* 係数が ``compute_cost`` 側 1.6 / ``payroll_dataframe`` 側 1.25 と二重定義だった
"""

from __future__ import annotations

from datetime import date, time

import pytest

from shiftai import exporter, gap_analysis, local_rules, solver
from shiftai.domain import (
    AgeClass,
    ChildPlan,
    Contract,
    EmploymentType,
    FacilitySettings,
    Role,
    StaffMember,
    cost_coefficient,
)
from shiftai.standards import build_requirements

STANDARD_KEY = "全国基準（厚労省）"
DAY = date(2026, 10, 5)
DAY_OPEN = time(9, 0)
DAY_CLOSE = time(15, 0)
RATE_DEFAULT = 1500.0
RATE_HIGH = 3000.0


def make_member(staff_id: str, employment=EmploymentType.SEI) -> StaffMember:
    return StaffMember(
        staff_id,
        "",
        (Role.HOIKUSHI,),
        Contract(weekly_hours=40.0, daily_hours=8.0, employment_type=employment),
    )


@pytest.fixture(scope="module")
def solved(request):
    """4 名の職員で 1 日を解いた結果と、その過不足レポート。"""
    children = [
        ChildPlan(f"C{i:03d}", "", DAY, AgeClass.AGE_3, DAY_OPEN, DAY_CLOSE)
        for i in range(4)
    ]
    staff = [make_member(f"S{i:03d}") for i in range(3)]
    table = build_requirements(
        children, [DAY], local_rules.get_standard(STANDARD_KEY),
        day_open=DAY_OPEN, day_close=DAY_CLOSE, granularity_min=30,
    )
    result = solver.solve_shift(
        children, staff, table, time_limit_sec=20,
        standard=local_rules.get_standard(STANDARD_KEY),
    )
    report = gap_analysis.analyze_gap(
        table, result, staff, standard=local_rules.get_standard(STANDARD_KEY)
    )
    return result, report, staff, table


# --- R1 / R2: settings の取り込み --------------------------------------------


def test_summarizeはsettingsの単価を使う(solved) -> None:
    """単価 3000 円で summarize の人件費が 2 倍になること（修正前の回帰防止）。"""
    result, report, staff, _table = solved
    low = gap_analysis.summarize(
        result, report, staff, FacilitySettings(labor_cost_per_hour=RATE_DEFAULT)
    )
    high = gap_analysis.summarize(
        result, report, staff, FacilitySettings(labor_cost_per_hour=RATE_HIGH)
    )
    assert high["人件費"] == pytest.approx(low["人件費"] * 2, rel=0.01)


def test_summarizeはsettings省略時に既定単価を使う(solved) -> None:
    """``settings=None`` のときは既定の 1500 円のままであること。"""
    result, report, staff, _table = solved
    explicit = gap_analysis.summarize(
        result, report, staff, FacilitySettings(labor_cost_per_hour=RATE_DEFAULT)
    )
    default = gap_analysis.summarize(result, report, staff)
    assert default["人件費"] == pytest.approx(explicit["人件費"])


# --- R3: compute_cost と payroll の基準一致 --------------------------------


def test_compute_costとpayrollの基準が一致する(solved) -> None:
    """同じ result / staff / settings で両者の人件費が一致すること。"""
    result, _report, staff, table = solved
    settings = FacilitySettings(labor_cost_per_hour=RATE_HIGH)
    cost = gap_analysis.compute_cost(result, staff, settings)
    payroll_total = int(exporter.payroll_dataframe(result, table.slots, staff, settings)[
        "推定人件費"
    ].sum())
    assert cost == pytest.approx(payroll_total, abs=1.0)


def test_payrollの実働時間は二重控除されない(solved) -> None:
    """実働時間は「勤務 − 休憩」でなく「勤務そのもの」であること。

    ``worked_min`` は ``CellState.WORK`` のみを集計しているので、
    さらに ``break_min`` を引くと休憩時間が二重に差し引かれる。
    """
    result, _report, staff, table = solved
    payroll = exporter.payroll_dataframe(result, table.slots, staff)
    assert len(payroll) == len(staff)
    for row in payroll.itertuples():
        assert row.実働時間 == pytest.approx(row.総勤務時間, abs=0.01), (
            f"実働時間 {row.実働時間} と総勤務時間 {row.総勤務時間} が一致していない"
        )


def test_compute_costは休憩を含まない(solved) -> None:
    """休憩（``CellState.BREAK``）は人件費に算入しないこと。"""
    result, _report, staff, _table = solved
    assert any(a.state.name == "BREAK" for a in result.assignments), "検証前提として休憩があること"
    worked_only = sum(
        a.slot.hours for a in result.assignments if a.state.name == "WORK"
    )
    with_break = sum(
        a.slot.hours for a in result.assignments if a.state.name != "OFF"
    )
    cost = gap_analysis.compute_cost(
        result, staff, FacilitySettings(labor_cost_per_hour=1.0)
    )
    ratio = with_break / worked_only
    # 係数が入るので絶対値では比較せず、休憩込み/実働-only の比で検証する
    assert cost < worked_only * ratio or cost <= worked_only * 1.25


# --- R4: 係数の一本化 ------------------------------------------------------


def test_人件費係数が一箇所に定義されている() -> None:
    """係数が ``domain`` にしか定義されていないこと（構造テスト）。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "shiftai"
    offenders = [
        path.name
        for path in src.rglob("*.py")
        if "_SE_FULL_TIME_FACTOR" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"旧定数が残っている: {offenders}"


@pytest.mark.parametrize(
    ("employment", "expected"),
    [
        (EmploymentType.SEI, 1.25),
        (EmploymentType.UKEIYOU, 1.15),
        (EmploymentType.PART, 1.0),
        (EmploymentType.BUNKIN, 1.0),
        (None, 1.0),
    ],
)
def test_cost_coefficientは雇用形態ごとの値を返す(employment, expected) -> None:
    assert cost_coefficient(employment) == expected


# --- R5: UI と summary.md の一致 --------------------------------------------


def test_summarizeとpayrollの人件費が一致する(solved) -> None:
    """``summarize()`` の人件費と給与 CSV の合計が同値であること。"""
    result, report, staff, table = solved
    settings = FacilitySettings(labor_cost_per_hour=RATE_HIGH)
    summary_cost = gap_analysis.summarize(result, report, staff, settings)["人件費"]
    payroll_total = int(exporter.payroll_dataframe(result, table.slots, staff, settings)[
        "推定人件費"
    ].sum())
    assert summary_cost == pytest.approx(payroll_total, abs=1.0)
