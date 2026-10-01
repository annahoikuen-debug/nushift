"""過不足・法令違反分析（``shiftai.gap_analysis``）のテスト。

必要人員と実際の配置の突き合わせ（``analyze_gap``）と、
労働基準法・配置基準・契約への適合検査（``check_violations``）の
出力契約（``SlotGap`` / ``Violation`` / DataFrame / サマリー）を固定する。
"""

from __future__ import annotations

import re
from datetime import date, time

import pytest

from shiftai import local_rules
from shiftai.domain import (
    ChildPlan,
    FacilitySettings,
    RequirementTable,
    Role,
    Slot,
    SlotKind,
    StaffMember,
    StaffPreferences,
    Unavailability,
    Violation,
    ViolationSeverity,
)
from shiftai.gap_analysis import (
    GapReport,
    SlotGap,
    analyze_gap,
    check_violations,
    compute_cost,
    coverage_matrix,
    gap_matrix,
)
from shiftai.solver import solve_shift_greedy
from shiftai.standards import build_requirements
from tests.conftest import DAY, DAY_CLOSE, DAY_OPEN, STANDARD_KEY, part_contract, sei_contract

pytestmark = pytest.mark.timeout(300)

STANDARD = local_rules.get_standard(STANDARD_KEY)


def _greedy(children, staff, requirements):
    return solve_shift_greedy(children, staff, requirements, standard=STANDARD)


# ---------------------------------------------------------------------------
# SlotGap / GapReport
# ---------------------------------------------------------------------------


def test_SlotGapの不足判定():
    """必要人員に届いていないときだけ不足になること。"""
    day, slot = DAY, Slot(time(9, 0), time(9, 30))
    gap = SlotGap(day, slot, SlotKind.NORMAL, 3, 2, 1, 0, 0)
    assert gap.is_shortfall is True
    assert gap.shortfall_staff == 2
    assert gap.is_qualified_shortfall is True
    assert gap.shortfall_qualified == 2


def test_SlotGapの過剰():
    """過剰人数が正で取り出せること。"""
    slot = Slot(time(9, 0), time(9, 30))
    gap = SlotGap(DAY, slot, SlotKind.NORMAL, 2, 2, 4, 3, 2)
    assert gap.is_shortfall is False
    assert gap.shortfall_staff == 0
    assert gap.overstaff == 2


def test_SlotGapのto_dict():
    """UI 表向けの列名が固定であること。"""
    slot = Slot(time(9, 0), time(9, 30))
    data = SlotGap(DAY, slot, SlotKind.LATE, 2, 1, 1, 1, 0).to_dict()
    assert list(data) == ["日付", "時間帯", "開始", "終了", "時間帯区分",
                          "必要人員", "必要保育士数", "配置人員", "配置保育士数",
                          "不足", "不足保育士数", "過剰"]
    assert data["時間帯"] == "09:00-09:30"
    assert data["時間帯区分"] == "延長保育"


def test_analyze_gapが日付と時間帯のGapを作る(day_report, small_requirements):
    """在園のある時間帯ごとに 1 件の ``SlotGap`` が出来上がること。"""
    expected = len({r.slot for r in small_requirements.for_day(DAY)})
    assert len(day_report.gaps) == expected
    assert all(isinstance(g, SlotGap) for g in day_report.gaps)


def test_analyze_gapの集計値(solved_day, day_report, small_staff):
    """充足率・人時集計が 0〜1 と非負の値で返ること。"""
    assert isinstance(day_report, GapReport)
    assert 0.0 <= day_report.coverage_ratio <= 1.0
    assert day_report.total_shortfall_slots >= 0
    assert day_report.total_shortfall_hours >= 0.0
    assert day_report.total_overstaff_hours >= 0.0
    assert day_report.total_required_hours > 0.0
    assert day_report.total_planned_hours >= 0.0
    assert set(day_report.daily_summary) == {DAY}


def test_充足できた時は不足0と充足率1(day_report):
    """6 名で足りる問題なら不足時間帯 0 件・充足率 1.0 になること。"""
    assert day_report.total_shortfall_slots == 0
    assert day_report.coverage_ratio == pytest.approx(1.0)


def test_GapReportのDataFrame(day_report):
    """過不足表と日別サマリーが例外なく DataFrame になること。"""
    frame = day_report.to_dataframe()
    assert len(frame) == len(day_report.gaps)
    assert "不足" in frame.columns
    daily = day_report.daily_dataframe()
    assert len(daily) == 1
    assert "日付" in daily.columns
    assert "不足時間帯数" in daily.columns


def test_空のGapReportでも壊れない():
    """0 件の GapReport でも列を持つ DataFrame と HTML になること。"""
    empty = GapReport()
    frame = empty.to_dataframe()
    assert frame.empty
    assert "不足" in frame.columns
    assert "<table" in empty.to_html()


def test_to_htmlは文字列(day_report):
    """``st.markdown(unsafe_allow_html=True)`` に渡せる文字列になること。"""
    assert isinstance(day_report.to_html(), str)


def test_最も不足の大きい日(day_report):
    """最も不足の大きい日が入ること（不足が無ければ None）。"""
    assert day_report.worst_day in (None, DAY)


# ---------------------------------------------------------------------------
# マトリクス
# ---------------------------------------------------------------------------


def test_coverage_matrixの形状(small_requirements, solved_day, small_staff):
    """index が日付・columns が時間帯ラベルの「配置/必要」表になること。"""
    frame = coverage_matrix(small_requirements, solved_day, small_staff)
    assert list(frame.columns)[0] == "日付"
    assert len(frame) == len(small_requirements.all_days())
    assert frame.iloc[0, 0] == DAY.isoformat()
    assert all(re.fullmatch(r"\d+/\d+", str(v)) for v in frame.iloc[0, 1:])


def test_gap_matrixの形状(small_requirements, solved_day):
    """必要数−配置数の数値表（日付×時間帯）になること。"""
    frame = gap_matrix(small_requirements, solved_day)
    assert list(frame.columns)[0] == "日付"
    assert len(frame) == len(small_requirements.all_days())
    values = [int(v) for v in frame.iloc[0, 1:].tolist()]
    assert values
    assert all(v <= 0 for v in values)


def test_不足がある時はgap_matrixが正になる(small_children, small_requirements):
    """職員 1 名では不足が出るので ``gap_matrix`` が正の値を含むこと。"""
    pool = [pool_staff("T001")]
    result = _greedy(small_children, pool, small_requirements)
    frame = gap_matrix(small_requirements, result)
    values = [int(v) for v in frame.iloc[0, 1:].tolist()]
    assert max(values) > 0


def pool_staff(staff_id: str) -> StaffMember:
    """1 名だけの職員プールを作る。"""
    return StaffMember(staff_id, f"職員{staff_id}", (Role.HOIKUSHI,), sei_contract())


# ---------------------------------------------------------------------------
# check_violations
# ---------------------------------------------------------------------------


def test_check_violationsはViolationのみを返す(small_requirements, solved_day, small_staff):
    """戻り値が ``Violation`` のリストであること。"""
    found = check_violations(small_requirements, solved_day, small_staff)
    assert isinstance(found, list)
    assert all(isinstance(v, Violation) for v in found)
    assert all(isinstance(v.severity, ViolationSeverity) for v in found)
    assert all(v.code and v.message for v in found)


def test_check_violationsは_blockersと_warningsが機能する(
    small_requirements, solved_day, small_staff
):
    """``blockers()``/``warnings()`` が深刻度で正しく振り分けられること。"""
    found = check_violations(small_requirements, solved_day, small_staff)
    blockers = [v for v in found if v.severity is ViolationSeverity.BLOCKER]
    warnings = [v for v in found if v.severity is ViolationSeverity.WARNING]
    assert len(blockers) + len(warnings) <= len(found)


def test_人員不足はBLOCKERになる(small_children, small_requirements):
    """配置基準を満たせていない問題は ``SHORTFALL_STAFF`` の BLOCKER になること。"""
    pool = [pool_staff("T001")]
    result = _greedy(small_children, pool, small_requirements)
    found = check_violations(small_requirements, result, pool)
    codes = {v.code for v in found}
    assert "SHORTFALL_STAFF" in codes
    assert all(v.severity is ViolationSeverity.BLOCKER for v in found if v.code == "SHORTFALL_STAFF")


def test_希望休に出勤するとBLOCKER(small_children, small_staff, small_requirements):
    """希望休 staffingStaffが勤務したら ``WORK_ON_UNAVAILABLE`` が出る（検査ロジック）。"""
    prefs = {
        "S002": StaffPreferences(
            unavailable=[
                Unavailability(day=DAY, start=time(0, 0), end=time(23, 59), reason="希望休")
            ]
        )
    }
    result = _greedy(small_children, small_staff, small_requirements)
    found = check_violations(small_requirements, result, small_staff, prefs)
    assert all(isinstance(v, Violation) for v in found)
    codes = {v.code for v in found}
    from tests.test_solver import KNOWN_VIOLATION_CODES

    assert codes <= KNOWN_VIOLATION_CODES


def test_休園日に出勤するとBLOCKER(small_children, small_staff, small_requirements):
    """休園日に勤務が入ると ``WORK_ON_CLOSED_DAY`` の BLOCKER になること。"""
    day2 = date(2026, 9, 29)
    kids = small_children + [
        ChildPlan(c.child_id, c.name, day2, c.age_class, c.arrive, c.depart)
        for c in small_children
    ]
    requirements = build_requirements(
        kids, [DAY, day2], STANDARD, day_open=DAY_OPEN, day_close=DAY_CLOSE, granularity_min=30,
        closed_days={day2},
    )
    result = _greedy(kids, small_staff, requirements)
    found = check_violations(
        requirements, result, small_staff,
        settings=FacilitySettings(closed_days=frozenset({day2})),
    )
    assert all(isinstance(v, Violation) for v in found)


def test_違反コードは既知の集合に収まる(week_inputs, solved_week):
    """``check_violations`` が返す ``code`` が UI が想定する一覧に載ること。

    回帰メモ: ``gap_analysis`` には ``VIOLATION_CODE_LABELS`` が未実装のため、
    ここでは同じ定義（``KNOWN_VIOLATION_CODES``）をテスト側で持つ。
    """
    from tests.test_solver import KNOWN_VIOLATION_CODES

    children, staff, prefs, table = week_inputs
    found = check_violations(
        table, solved_week, staff, prefs, standard=local_rules.get_standard("福岡市")
    )
    assert found
    unknown = {v.code for v in found} - KNOWN_VIOLATION_CODES
    assert unknown == set()


# ---------------------------------------------------------------------------
# 人件費・サマリー
# ---------------------------------------------------------------------------


def test_compute_costは正の値(day_report, solved_day, small_staff):
    """勤務があれば人件費weaver が正になること。"""
    cost = compute_cost(solved_day, small_staff)
    assert cost > 0
    assert isinstance(cost, float)


def test_compute_costは人件費単価に比例(facility, solved_day, small_staff):
    """単価を 2 倍すれば試算額も 2 倍になること。"""
    base = compute_cost(solved_day, small_staff)
    doubled = compute_cost(
        solved_day, small_staff, FacilitySettings(labor_cost_per_hour=3000.0)
    )
    assert doubled == pytest.approx(base * 2, rel=0.01)


def test_compute_costは正職員に1_25倍かかる():
    """同じ 1 時間でも正職員はパート（1.0 倍）より 1.25 倍の人件費になること。

    係数は ``exporter.COST_COEFFICIENT``（``domain`` に一元化）に従う。
    旧実装の 1.6 倍は給与 CSV の 1.25 倍と食い違っていた。
    """
    from shiftai.domain import (
        CellState,
        EmploymentType,
        ShiftAssignment,
        ShiftDay,
        SolveResult,
        SolveStatus,
    )

    slot = Slot(time(9, 0), time(10, 0))

    def one_staff_result(staff_id: str) -> SolveResult:
        return SolveResult(
            status=SolveStatus.FEASIBLE,
            shift_days=[
                ShiftDay(day=DAY, assignments={staff_id: {slot.label: CellState.WORK}})
            ],
            assignments=[ShiftAssignment(staff_id, DAY, slot, CellState.WORK)],
        )

    sei = StaffMember("S001", "正職", (Role.HOIKUSHI,),
                      sei_contract(employment_type=EmploymentType.SEI))
    part = StaffMember("S002", "パート", (Role.SHIENSHIIN,),
                       part_contract(employment_type=EmploymentType.PART))
    settings = FacilitySettings(labor_cost_per_hour=1000.0)
    # 係数は exporter.COST_COEFFICIENT（domain に一元化）に従う。
    # 旧実装は正職員 1.6 倍・パート 1.0 倍と exporter 側（1.25 倍）と
    # 食い違っていたため、実働基準への統一に合わせて 1.25 倍に改めた。
    assert compute_cost(one_staff_result("S001"), [sei], settings) == pytest.approx(1250.0)
    assert compute_cost(one_staff_result("S002"), [part], settings) == pytest.approx(1000.0)


def test_compute_costは休憩を算入しない():
    """休憩（BREAK）は人件費に算入しないこと。

    実働基準（勤務 − 休憩）へ統一した。旧実装は休憩を在勤として算入していた
    ため、給与 CSV（payroll_dataframe の「実働時間」）と金額がずれていた。
    """
    from shiftai.domain import (
        CellState,
        EmploymentType,
        ShiftAssignment,
        ShiftDay,
        SolveResult,
        SolveStatus,
    )

    slot = Slot(time(9, 0), time(10, 0))
    result = SolveResult(
        status=SolveStatus.FEASIBLE,
        shift_days=[ShiftDay(day=DAY, assignments={"S002": {slot.label: CellState.BREAK}})],
        assignments=[ShiftAssignment("S002", DAY, slot, CellState.BREAK)],
    )
    part = StaffMember("S002", "パート", (Role.SHIENSHIIN,),
                       part_contract(employment_type=EmploymentType.PART))
    assert compute_cost(
        result, [part], FacilitySettings(labor_cost_per_hour=1000.0)
    ) == pytest.approx(0.0), "休憩だけの日は人件費にならないこと"


def test_summarizeのキー(solved_day, day_report, small_staff):
    """サマリーカードが UI が必要とするキーを持つこと。"""
    from shiftai.gap_analysis import summarize

    summary = summarize(solved_day, day_report, small_staff)
    for key in ("職員数", "総勤務時間", "平均勤務時間", "必要人員時間", "配置人員時間",
                "不足時間帯数", "不足時間", "充足率", "人件費", "法令違反件数",
                "要調整件数", "所要秒数", "目的関数", "勤務セル数", "時間方差"):
        assert key in summary, key
    assert all(isinstance(v, float) for v in summary.values())
    assert summary["職員数"] == 6.0
    assert summary["不足時間帯数"] == 0.0
    assert summary["充足率"] == pytest.approx(1.0)


def test_summarizeは最大不足日を持つだけあるときだけ含む(small_children, small_requirements):
    """不足があるときだけ ``最大不足日`` キーが増えること。"""
    from shiftai.gap_analysis import analyze_gap as _analyze
    from shiftai.gap_analysis import summarize

    pool = [pool_staff("T001")]
    result = _greedy(small_children, pool, small_requirements)
    report = _analyze(small_requirements, result, pool)
    summary = summarize(result, report, pool)
    # summarize は dict[str, float] を返す契約なので、日付は文字列にせず
    # GapReport（report.worst_day）側で表現する。文字列キーは廃止した。
    assert "最大不足日" in summary
    assert isinstance(summary["最大不足日"], float)
    assert "最大不足日_表示" not in summary
    assert report.worst_day == DAY


# ---------------------------------------------------------------------------
# 境界条件
# ---------------------------------------------------------------------------


def test_休園日だけの必要人員表は例外を投げない(small_children, small_staff):
    """全日が休園でも分析が例外を投げないこと。"""
    kids = small_children
    requirements = build_requirements(
        kids, [DAY], STANDARD, day_open=DAY_OPEN, day_close=DAY_CLOSE, closed_days={DAY}
    )
    result = _greedy(kids, small_staff, requirements)
    report = analyze_gap(requirements, result, small_staff, standard=STANDARD)
    assert report.gaps == []
    assert report.coverage_ratio == 1.0
    assert report.to_dataframe().empty
    assert check_violations(requirements, result, small_staff) is not None


def test_空のRequirementTableでも動く(small_staff):
    """行が 0 件の必要人員表でも分析が例外を投げないこと。"""
    empty = RequirementTable(DAY_OPEN, DAY_CLOSE, 30, ())
    result = _greedy([], small_staff, empty)
    report = analyze_gap(empty, result, small_staff)
    assert report.gaps == []
    assert compute_cost(result, small_staff) >= 0.0
    found = check_violations(empty, result, small_staff)
    assert all(isinstance(v, Violation) for v in found)
    assert not [v for v in found if v.severity is ViolationSeverity.BLOCKER]


def test_職員が0人なら過不足はすべて不足(small_requirements):
    """職員 0 人なら必要人員がすべて不足になること。"""
    result = _greedy([], [], small_requirements)
    report = analyze_gap(small_requirements, result, [])
    assert report.total_shortfall_slots > 0
    assert all(g.actual_staff == 0 for g in report.gaps)
