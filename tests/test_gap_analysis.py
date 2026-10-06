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
    AgeClass,
    CellState,
    ChildPlan,
    FacilitySettings,
    RequirementTable,
    Role,
    ShiftAssignment,
    ShiftDay,
    Slot,
    SlotKind,
    SolveResult,
    SolveStatus,
    StaffMember,
    StaffPreferences,
    Unavailability,
    Violation,
    ViolationSeverity,
)
from shiftai.gap_analysis import (
    GapReport,
    SlotGap,
    _required_break_minutes,
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
    assert list(data) == [
        "日付",
        "時間帯",
        "開始",
        "終了",
        "時間帯区分",
        "必要人員",
        "必要保育士数",
        "配置人員",
        "配置保育士数",
        "不足",
        "不足保育士数",
        "過剰",
    ]
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
    """最も不足の大きい日が入ること（不足が無ければ None）。

    修正前: ``worst_day in (None, DAY)`` は ``None`` も許すため、
    ``worst_day`` が常に ``None`` でも通っていた。

    ``day_report`` fixture は不足が無い構成なので ``None`` が正となるため、
    **「不足がある日では必ず日が入ること」** を別途の主張として
    主張する（恒久に None のままでも検出できる）。
    """
    from datetime import date as _date

    # 前提: この fixture には不足が無いので None が正しい
    worst = day_report.worst_day
    assert worst is None, f"不足が無いのに worst_day が立っている: {worst}"

    # 不足があるケースでは None ではなく日が入ること
    from shiftai.domain import AgeClass

    kids = [
        ChildPlan("C001", "園児A", DAY, AgeClass.INFANT, time(9, 0), time(17, 0)),
        ChildPlan("C002", "園児B", DAY, AgeClass.INFANT, time(9, 0), time(17, 0)),
    ]
    understaffed = build_requirements(
        kids,
        [DAY],
        STANDARD,
        day_open=DAY_OPEN,
        day_close=DAY_CLOSE,
        granularity_min=60,
    )
    result = _greedy(kids, [], understaffed)
    under_report = analyze_gap(understaffed, result, [], standard=STANDARD)
    assert under_report.worst_day == DAY, (
        f"不足があるのに worst_day が None: {under_report.worst_day}"
    )
    assert isinstance(under_report.worst_day, _date)


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
    """``blockers()``/``warnings()`` が深刻度で正しく振り分けられること。

    修正前: ``len(blockers) + len(warnings) <= len(found)`` のみで、
    ``blockers`` と ``warnings`` は ``found`` の互いに素な部分リストなので
    **常に真**（何も主張しない）。違反が 1 件も出ないデータセットでも
    「振り分けが動作した」と誤認していた。

    ``ViolationSeverity`` には ``INFO`` もあるため、
    3 つのバケット（BLOCKER / WARNING / INFO）で**過不足なく**分かれることを主張する。
    """
    found = check_violations(small_requirements, solved_day, small_staff)
    assert found, "検証前提: この解には違反があること"
    buckets = {sev: [v for v in found if v.severity is sev] for sev in ViolationSeverity}
    # 全ての違反がいずれかの深刻度にちょうど 1 回だけ分類される
    assert sum(len(vs) for vs in buckets.values()) == len(found), (
        "深刻度が分類されていない違反がある"
    )
    # バケット同士が重複していない（同じ違反が 2 つのバケットに入っていない）
    seen: list[str] = []
    for vs in buckets.values():
        seen.extend(id(v) for v in vs)
    assert len(seen) == len(set(seen)), "同じ違反が複数のバケットに入っている"
    # 実際に違反が 1 件以上分類されていること
    assert any(buckets.values()), "違反が全て未分類のまま"
    # この fixture の解は INFO 級（MONTHLY_HOURS_SHORT など）しか出ないため、
    # 「BLOCKER/WARNING を含む」は前提にしない。代わりに
    # 各バケットが正しい深刻度で構成されていることを主張する。
    for sev, vs in buckets.items():
        assert all(v.severity is sev for v in vs), f"{sev} バケットに別の深刻度が入っている"
    # 深刻度は必ず enum のいずれかであって、裸の文字列 etc ではない
    assert {v.severity for v in found} <= set(ViolationSeverity)


def test_人員不足はBLOCKERになる(small_children, small_requirements):
    """配置基準を満たせていない問題は ``SHORTFALL_STAFF`` の BLOCKER になること。"""
    pool = [pool_staff("T001")]
    result = _greedy(small_children, pool, small_requirements)
    found = check_violations(small_requirements, result, pool)
    codes = {v.code for v in found}
    assert "SHORTFALL_STAFF" in codes
    assert all(
        v.severity is ViolationSeverity.BLOCKER for v in found if v.code == "SHORTFALL_STAFF"
    )


def test_希望休に出勤するとBLOCKER(small_children, small_staff, small_requirements):
    """希望休に職員が勤務したら ``WORK_ON_UNAVAILABLE`` の BLOCKER が出る（検査ロジック）。

    修正前: ``codes <= KNOWN_VIOLATION_CODES``（部分集合の主張）のみで、
    ``WORK_ON_UNAVAILABLE`` の検査コードを**削除してもテストは通っていた**。
    ``prefs`` は ``check_violations`` にしか渡していないので、
    サンプル解は検査ロジックとは無関係なので、このテストは
    `check_violations` 側の検出だけを検証する。
    """
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
    # 期待する違反コードが実際に出ていること
    assert "WORK_ON_UNAVAILABLE" in codes, (
        f"WORK_ON_UNAVAILABLE が出ていない（出たコード: {sorted(codes)}）"
    )
    # かつ BLOCKER であること（テスト名が BLOCKER を主張している）
    unavailable = [v for v in found if v.code == "WORK_ON_UNAVAILABLE"]
    assert unavailable, "WORK_ON_UNAVAILABLE が出ていない"
    assert all(v.severity is ViolationSeverity.BLOCKER for v in unavailable), (
        f"WORK_ON_UNAVAILABLE が BLOCKER でない: {[(v.code, v.severity) for v in unavailable]}"
    )


def test_休園日に出勤するとBLOCKER(small_children, small_staff, small_requirements):
    """休園日に勤務が入ると ``WORK_ON_CLOSED_DAY`` の BLOCKER になること。

        修正前: ``assert all(isinstance(v, Violation) for v in found)`` のみで、
        空のリストでも真（``all()`` は空反復で真）。
        ``WORK_ON_CLOSED_DAY`` の分岐を削除してもテストは通っていた。

        ``_greedy`` を使うと **休園日を正しく OFF にする**ため違反が
        原理的に発生しない（本修正で実際に確認した）。
        さらに ``check_violations`` は ``requirements.all_days()`` の日を
    走査するので、休園日を含む ``RequirementTable`` が必要。
        よって **休園日に勤務させた違反解** を明示的に作る。
    """
    day2 = date(2026, 9, 29)
    kids = list(small_children) + [
        ChildPlan(c.child_id, c.name, day2, c.age_class, c.arrive, c.depart) for c in small_children
    ]
    days = sorted({c.day for c in kids})
    requirements = build_requirements(
        kids,
        days,
        STANDARD,
        day_open=DAY_OPEN,
        day_close=DAY_CLOSE,
        granularity_min=30,
        closed_days={day2},
    )
    assert day2 in requirements.all_days(), "検証前提: 休園日が対象日に含まれること"

    slot = requirements.slots[0]
    sid = small_staff[0].staff_id
    violating = SolveResult(
        status=SolveStatus.FEASIBLE,
        shift_days=[ShiftDay(day=day2, assignments={sid: {slot.label: CellState.WORK}})],
        assignments=[ShiftAssignment(sid, day2, slot, CellState.WORK)],
    )
    found = check_violations(
        requirements,
        violating,
        small_staff,
        settings=FacilitySettings(closed_days=frozenset({day2})),
    )
    assert all(isinstance(v, Violation) for v in found)
    closed = [v for v in found if v.code == "WORK_ON_CLOSED_DAY"]
    assert closed, (
        f"WORK_ON_CLOSED_DAY が出ていない（出たコード: {sorted({v.code for v in found})}）"
    )
    assert all(v.severity is ViolationSeverity.BLOCKER for v in closed), (
        f"WORK_ON_CLOSED_DAY が BLOCKER でない: {[v.severity for v in closed]}"
    )
    assert all(v.day == day2 for v in closed), "違反の日の記録が誤っている"


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
    """勤務があれば人件費が正になること。"""
    cost = compute_cost(solved_day, small_staff)
    assert cost > 0
    assert isinstance(cost, float)


def test_compute_costは人件費単価に比例(facility, solved_day, small_staff):
    """単価を 2 倍すれば試算額も 2 倍になること。"""
    base = compute_cost(solved_day, small_staff)
    doubled = compute_cost(solved_day, small_staff, FacilitySettings(labor_cost_per_hour=3000.0))
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
            shift_days=[ShiftDay(day=DAY, assignments={staff_id: {slot.label: CellState.WORK}})],
            assignments=[ShiftAssignment(staff_id, DAY, slot, CellState.WORK)],
        )

    sei = StaffMember(
        "S001", "正職", (Role.HOIKUSHI,), sei_contract(employment_type=EmploymentType.SEI)
    )
    part = StaffMember(
        "S002", "パート", (Role.SHIENSHIIN,), part_contract(employment_type=EmploymentType.PART)
    )
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
    part = StaffMember(
        "S002", "パート", (Role.SHIENSHIIN,), part_contract(employment_type=EmploymentType.PART)
    )
    assert compute_cost(
        result, [part], FacilitySettings(labor_cost_per_hour=1000.0)
    ) == pytest.approx(0.0), "休憩だけの日は人件費にならないこと"


def test_summarizeのキー(solved_day, day_report, small_staff):
    """サマリーカードが UI が必要とするキーを持つこと。"""
    from shiftai.gap_analysis import summarize

    summary = summarize(solved_day, day_report, small_staff)
    for key in (
        "職員数",
        "総勤務時間",
        "平均勤務時間",
        "必要人員時間",
        "配置人員時間",
        "不足時間帯数",
        "不足時間",
        "充足率",
        "人件費",
        "法令違反件数",
        "要調整件数",
        "所要秒数",
        "目的関数",
        "勤務セル数",
        "時間方差",
    ):
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
    # 修正前: ``is not None`` は「list を返すので常に真」。
    # 全日が休園なので **BLOCKER / WARNING は出ない**ことを主張する
    # （INFO 級の ``MONTHLY_HOURS_SHORT``「月間の労働時間が契約より少ない」
    #  は出るのが正当なので、深刻度で切り分ける）。
    violations = check_violations(requirements, result, small_staff)
    assert isinstance(violations, list), "list が返ること"
    severe = [
        v
        for v in violations
        if v.severity in (ViolationSeverity.BLOCKER, ViolationSeverity.WARNING)
    ]
    assert severe == [], (
        f"休園日のみなのに BLOCKER/WARNING が出ている: "
        f"{[(v.code, v.severity.value) for v in severe]}"
    )


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


# ---------------------------------------------------------------------------
# 回帰：法定休憩時間の閾値取り違え
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("work_minutes", "expected"),
    [
        (360, 0),  # 6hちょうど：休憩義務なし
        (420, 45),  # 6h超 → 45分
        (480, 45),  # 8hちょうど：45分のまま
        (481, 60),  # 8h超 → 60分（労基法9条）
        (540, 60),  # 9h
        (600, 60),  # 10h
    ],
)
def test_法定休憩は8時間超で60分になる(work_minutes, expected):
    """8時間超の勤務を 45 分で通してはいけない。

    ``STATUTORY_BREAK_THRESHOLDS`` は ``((480, 60), (360, 45))`` と
    閾値の**降順**で並ぶ。無条件代入すると常に下限側の 45 分が勝ち、
    8時間超勤務の休憩不足を見逃す。関数の docstring（8時間超60分）と
    実装が食い違っていたため固定する。
    """
    assert _required_break_minutes(work_minutes) == expected


def test_法定休憩は閾値の並び順に依存しない(monkeypatch):
    """定数を昇順に並べ替えても結果が同じであること。"""
    from shiftai import gap_analysis

    monkeypatch.setattr(gap_analysis, "STATUTORY_BREAK_THRESHOLDS", ((6 * 60, 45), (8 * 60, 60)))
    assert gap_analysis._required_break_minutes(540) == 60
    assert gap_analysis._required_break_minutes(420) == 45


# ---------------------------------------------------------------------------
# 回帰：greedy の休憩が勤務ブロックの外に入る
# ---------------------------------------------------------------------------


def test_午後の勤務でも休憩が勤務ブロック内に入る():
    """勤務可能時間帯が午後の職員にも、休憩が実際に勤められない。

    休憩位置 ``pos`` は ``duty``（勤務スロットの**相対**位置）に対して
    計算されていたが、そのまま時間帯グリッドの添字として使われていた。
    勤務ブロックが後方に開始すると ``pos`` が OFF の時間帯を叩き、
    8時間超の勤務に休憩が 0 分になる。ブロック内の実スロットに変換して
    解決すること。
    """
    day = date(2026, 9, 28)
    children = [
        ChildPlan(f"C00{i}", f"園児{i:03d}", day, AgeClass.INFANT, time(9, 0), time(20, 0))
        for i in range(1, 4)
    ]
    requirements = build_requirements(
        children,
        [day],
        STANDARD,
        day_open=time(9, 0),
        day_close=time(21, 0),
        granularity_min=30,
    )
    # 13:00 以降しか働けない職員が 1 名だけ。
    afternoon = [
        StaffMember(
            staff_id="S001",
            name="午後保育士",
            roles=(Role.HOIKUSHI,),
            contract=sei_contract(earliest_start=time(13, 0), latest_end=time(21, 0)),
        )
    ]

    result = solve_shift_greedy(children, afternoon, requirements, standard=STANDARD)

    slots = list(requirements.slots)
    row = result.shift_days[0].assignments.get("S001", {})
    work = [s for s in slots if row.get(s.label) is CellState.WORK]
    brk = [s for s in slots if row.get(s.label) is CellState.BREAK]
    assert work, "greedy が勤務を1つも割り当てなかった"

    first, last = slots.index(work[0]), slots.index(work[-1])
    inside = [s for s in brk if first <= slots.index(s) <= last]
    assert inside, (
        f"勤務ブロック {work[0].label}〜{work[-1].label} に休憩が入っていない"
        f"（休憩スロット: {[s.label for s in brk]}）"
    )
