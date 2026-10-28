"""改善案 1（HiGHS & ウォームスタート）、案 6（変形労働制・36協定・割増賃金）、案 8（現場用シフト記号 & パターンモード）の検証テスト。"""

from __future__ import annotations

import io
import zipfile
from datetime import date, time

import pandas as pd
import pytest
from openpyxl import load_workbook

from shiftai import local_rules
from shiftai.data_loader import load_staff
from shiftai.domain import (
    CellState,
    Contract,
    FacilitySettings,
    Role,
    ShiftAssignment,
    ShiftDay,
    Slot,
    StaffMember,
    ViolationSeverity,
)
from shiftai.exporter import (
    export_bundle_zip,
    payroll_dataframe,
    symbol_matrix_dataframe,
)
from shiftai.gap_analysis import check_violations
from shiftai.shift_patterns import (
    DEFAULT_PATTERNS,
    ShiftPattern,
    match_pattern,
    parse_pattern_spec,
)
from shiftai.solver import (
    _daily_cap_minutes,
    _get_solver,
    is_highs_available,
    solve_shift,
)
from shiftai.standards import build_requirements

OVERTIME_NOT_ALLOWED = "OVERTIME_NOT_ALLOWED"
VARIABLE_HOURS_ADAPTED = "VARIABLE_HOURS_ADAPTED"
VARIABLE_HOURS_EXCEEDED = "VARIABLE_HOURS_EXCEEDED"

DAYS = [date(2026, 10, 1), date(2026, 10, 2)]
OPEN = time(7, 30)
CLOSE = time(19, 30)


@pytest.fixture
def test_requirements(small_children):
    return build_requirements(
        small_children,
        DAYS,
        local_rules.get_standard("全国基準（厚労省）"),
        day_open=OPEN,
        day_close=CLOSE,
    )


# ===========================================================================
# 改善案 1: HiGHS ソルバー & ウォームスタート
# ===========================================================================


def test_highs_availability() -> None:
    """HiGHS が環境で利用可能であること。"""
    available = is_highs_available()
    assert isinstance(available, bool)


def test_solver_selection() -> None:
    """_get_solver が auto, highs, cbc を正しく取得できること。"""
    solver_auto, name_auto = _get_solver(limit_sec=5, msg=False, solver_name="auto")
    assert name_auto in ("HiGHS", "PULP_CBC_CMD")
    assert solver_auto is not None

    if is_highs_available():
        solver_highs, name_highs = _get_solver(limit_sec=5, msg=False, solver_name="highs")
        assert name_highs == "HiGHS"

    solver_cbc, name_cbc = _get_solver(limit_sec=5, msg=False, solver_name="cbc")
    assert name_cbc == "PULP_CBC_CMD"


def test_solve_shift_with_highs_if_available(small_children, small_requirements, small_staff) -> None:
    """HiGHS を指定して solve_shift が解を求められること。"""
    if not is_highs_available():
        pytest.skip("HiGHS がインストールされていないためスキップ")

    result = solve_shift(
        small_children,
        small_staff,
        small_requirements,
        time_limit_sec=10,
        solver_name="highs",
        warm_start=True,
    )
    print("DEBUG STATS:", result.stats)
    print("DEBUG MESSAGES:", result.messages)
    assert result.ok is True
    assert result.stats.get("solver") == "HiGHS"
    assert result.stats.get("warm_start") is True


def test_solve_shift_warm_start_behavior(small_children, small_requirements, small_staff) -> None:
    """ウォームスタート（初期解投入）の有無による動作検証。"""
    # warm_start=True
    res_warm = solve_shift(
        small_children,
        small_staff,
        small_requirements,
        time_limit_sec=10,
        warm_start=True,
    )
    assert res_warm.ok is True
    assert "warm_start_applied" in res_warm.stats

    # warm_start=False
    res_cold = solve_shift(
        small_children,
        small_staff,
        small_requirements,
        time_limit_sec=10,
        warm_start=False,
    )
    assert res_cold.ok is True
    assert res_cold.stats.get("warm_start") is False


# ===========================================================================
# 改善案 6: 1か月変形労働制・36協定・割増賃金シミュレーション
# ===========================================================================


def test_36_agreement_caps_daily_hours() -> None:
    """36協定未締結者は1日8時間（480分）でハードキャップされること。"""
    # 36協定あり（デフォルト）: daily_hours 9.0h -> 法定上限（600分）まで許容
    c_with = Contract(weekly_hours=40.0, daily_hours=9.0, has_36_agreement=True)
    assert _daily_cap_minutes(c_with) > 480

    # 36協定なし: daily_hours 9.0h でも 480分（8h）に制限
    c_without = Contract(weekly_hours=40.0, daily_hours=9.0, has_36_agreement=False)
    assert _daily_cap_minutes(c_without) == 480


def test_gap_analysis_36_agreement_blocker(test_requirements) -> None:
    """36協定未締結者の8時間超勤務が BLOCKER (OVERTIME_NOT_ALLOWED) として検出されること。"""
    # 36協定なしの職員
    st = StaffMember(
        staff_id="S01",
        name="テスト職員",
        roles=(Role.HOIKUSHI,),
        contract=Contract(
            weekly_hours=40.0,
            daily_hours=8.0,
            has_36_agreement=False,
            overtime_allowed=True,
        ),
    )
    day = DAYS[0]
    slots = list(test_requirements.slots)

    # 8.5時間（17スロット×30分 = 510分）勤務させる割当を作成
    from shiftai.domain import SolveResult, SolveStatus

    assign_list = [
        ShiftAssignment(staff_id="S01", day=day, slot=sl, state=CellState.WORK if i < 17 else CellState.OFF)
        for i, sl in enumerate(slots)
    ]

    shift_days = [ShiftDay(day=d) for d in DAYS]
    result = SolveResult(
        status=SolveStatus.OPTIMAL,
        shift_days=shift_days,
        assignments=assign_list,
    )

    violations = check_violations(
        test_requirements,
        result,
        [st],
        period_days=len(DAYS),
    )

    overtime_blockers = [v for v in violations if v.code == OVERTIME_NOT_ALLOWED]
    assert len(overtime_blockers) >= 1
    assert overtime_blockers[0].severity == ViolationSeverity.BLOCKER
    assert "36協定" in overtime_blockers[0].message


def test_gap_analysis_variable_working_hours(test_requirements) -> None:
    """変形労働制適用時、期間総枠超過が VARIABLE_HOURS_EXCEEDED として検出されること。"""
    from shiftai.domain import SolveResult, SolveStatus

    # 変形労働制ありの職員（2日間の期間で総枠は 40 * 2 / 7 = 11.43 時間）
    st = StaffMember(
        staff_id="S_VAR",
        name="変形職員",
        roles=(Role.HOIKUSHI,),
        contract=Contract(
            weekly_hours=40.0,
            daily_hours=8.0,
            has_36_agreement=True,
            variable_working_hours=True,
        ),
    )
    slots = list(test_requirements.slots)
    # 2日間で各8時間（計16時間 > 法定枠11.43時間）勤務
    assign_list = [
        ShiftAssignment(staff_id="S_VAR", day=d, slot=sl, state=CellState.WORK if i < 16 else CellState.OFF)
        for d in DAYS
        for i, sl in enumerate(slots)
    ]

    shift_days = [ShiftDay(day=d) for d in DAYS]
    result = SolveResult(
        status=SolveStatus.OPTIMAL,
        shift_days=shift_days,
        assignments=assign_list,
    )

    violations = check_violations(
        test_requirements,
        result,
        [st],
        period_days=len(DAYS),
    )

    var_codes = [v.code for v in violations]
    assert any(c in (VARIABLE_HOURS_ADAPTED, VARIABLE_HOURS_EXCEEDED) for c in var_codes)


def test_payroll_dataframe_overtime_night_holiday_calculation() -> None:
    """payroll_dataframe で深夜・時間外・休日時間および割増人件費が正しく計算されること。"""
    from shiftai.domain import SolveResult, SolveStatus

    st = StaffMember(
        staff_id="S01",
        name="給与テスト職員",
        roles=(Role.HOIKUSHI,),
        contract=Contract(weekly_hours=40.0, daily_hours=8.0),
    )
    # スロット定義: 朝7:00から夜23:00まで（22:00〜23:00は深夜）
    slots = [
        Slot(
            start=time(h, m),
            end=time(h if m == 0 else (h + 1) % 24, 30 if m == 0 else 0),
        )
        for h in range(7, 23)
        for m in (0, 30)
    ]
    day1 = date(2026, 10, 2)  # 金曜日（平日）
    day2 = date(2026, 10, 4)  # 日曜日（休日）
    settings = FacilitySettings(holiday_dates=frozenset({day2}))

    # day1 (平日): 8:00〜18:00まで勤務（実働10時間 = 所定8h + 時間外2h）
    # day2 (休日): 20:00〜23:00まで勤務（実働3時間 = 休日3h, うち22:00-23:00は深夜1h）
    assignments = {}
    for sl in slots:
        work1 = sl.start_minutes >= 8 * 60 and sl.end_minutes <= 18 * 60
        assignments[("S01", day1, sl.label)] = CellState.WORK if work1 else CellState.OFF
        work2 = sl.start_minutes >= 20 * 60 and sl.end_minutes <= 23 * 60
        assignments[("S01", day2, sl.label)] = CellState.WORK if work2 else CellState.OFF

    shift_days = [ShiftDay(day=day1), ShiftDay(day=day2)]
    result = SolveResult(
        status=SolveStatus.OPTIMAL,
        shift_days=shift_days,
        assignments=assignments,
    )

    df = payroll_dataframe(result, slots, [st], settings)
    assert len(df) == 1
    row = df.iloc[0]

    # 時間外時間、深夜時間、休日時間の存在確認
    assert "所定内時間" in df.columns
    assert "時間外時間" in df.columns
    assert "深夜時間" in df.columns
    assert "休日時間" in df.columns
    assert "割増人件費" in df.columns

    assert row["実働時間"] == 13.0
    assert row["所定内時間"] == 8.0
    assert row["時間外時間"] == 2.0
    assert row["深夜時間"] == 1.0
    assert row["休日時間"] == 3.0
    # 割増人件費が基本人件費より大きいこと
    assert row["割増人件費"] > row["推定人件費"]


def test_data_loader_staff_36_and_variable_hours() -> None:
    """CSVから36協定・変形労働制の指定が正しく読み込めること。"""
    csv_data = """職員ID,氏名,資格（主）,資格（副）,雇用形態,週契約時間,1日契約時間,36協定,変形労働制
S01,山田花子,保育士,,正職員,40,8,なし,有
S02,佐藤太郎,保育士,,パート,20,4,1,0
"""
    df = pd.read_csv(io.StringIO(csv_data))
    members, issues = load_staff(df)

    assert len(members) == 2
    assert members[0].contract.has_36_agreement is False
    assert members[0].contract.variable_working_hours is True

    assert members[1].contract.has_36_agreement is True
    assert members[1].contract.variable_working_hours is False


# ===========================================================================
# 改善案 8: 現場用シフト記号 & パターンモード
# ===========================================================================


def test_shift_pattern_symbols_and_parsing() -> None:
    """ShiftPattern の symbol 属性と parse_pattern_spec の解釈検証。"""
    # 直接生成
    p1 = ShiftPattern("early", "早番", time(7, 30), time(16, 30), symbol="早")
    assert p1.symbol == "早"
    assert p1.effective_symbol == "早"

    p2 = ShiftPattern("middle", "中番", time(8, 30), time(17, 30))
    assert p2.symbol == ""
    assert p2.effective_symbol == "中番"

    # parse_pattern_spec: 早番[早]=07:30-16:30
    p3 = parse_pattern_spec("早番[早]=07:30-16:30")
    assert p3.label == "早番"
    assert p3.symbol == "早"
    assert p3.effective_symbol == "早"
    assert p3.start == time(7, 30)
    assert p3.end == time(16, 30)

    # parse_pattern_spec: A勤:A=08:00-17:00
    p4 = parse_pattern_spec("A勤:A=08:00-17:00")
    assert p4.label == "A勤"
    assert p4.symbol == "A"
    assert p4.effective_symbol == "A"


def test_symbol_matrix_dataframe_output(test_requirements, small_staff) -> None:
    """symbol_matrix_dataframe が現場用の記号マトリクス表を正しく生成すること。"""
    from shiftai.domain import SolveResult, SolveStatus

    slots = list(test_requirements.slots)
    days = DAYS
    patterns = (
        ShiftPattern("early", "早番", time(7, 30), time(16, 30), symbol="早"),
        ShiftPattern("day", "日勤", time(8, 30), time(17, 30), symbol="日"),
    )

    # 1人目は初日早番、2日目休み
    # 2人目は初日休み、2日目日勤
    assignments = {}
    st1 = small_staff[0].staff_id
    st2 = small_staff[1].staff_id

    for sl in slots:
        # st1 day1 早番 (07:30〜16:30)
        assignments[(st1, days[0], sl.label)] = (
            CellState.WORK if 7 * 60 + 30 <= sl.start_minutes < 16 * 60 + 30 else CellState.OFF
        )
        # st1 day2 休み
        assignments[(st1, days[1], sl.label)] = CellState.OFF

        # st2 day1 休み
        assignments[(st2, days[0], sl.label)] = CellState.OFF
        # st2 day2 日勤 (08:30〜17:30)
        assignments[(st2, days[1], sl.label)] = (
            CellState.WORK if 8 * 60 + 30 <= sl.start_minutes < 17 * 60 + 30 else CellState.OFF
        )

    shift_days = [ShiftDay(day=d) for d in days]
    result = SolveResult(
        status=SolveStatus.OPTIMAL,
        shift_days=shift_days,
        assignments=assignments,
    )

    df = symbol_matrix_dataframe(result, small_staff[:2], slots=slots, patterns=patterns)
    assert len(df) == 2
    assert "職員ID" in df.columns
    assert "氏名" in df.columns
    assert "出勤日数" in df.columns
    assert "実働時間" in df.columns

    # 1人目の確認
    row1 = df[df["職員ID"] == st1].iloc[0]
    assert row1["出勤日数"] == 1
    assert row1["実働時間"] == 9.0
    # 初日「早」、2日目「休」
    day1_col = [c for c in df.columns if "10/1" in c or "10/01" in c][0]
    day2_col = [c for c in df.columns if "10/2" in c or "10/02" in c][0]
    assert row1[day1_col] == "早"
    assert row1[day2_col] == "休"

    # 2人目の確認
    row2 = df[df["職員ID"] == st2].iloc[0]
    assert row2["出勤日数"] == 1
    assert row2[day1_col] == "休"
    assert row2[day2_col] == "日"


def test_solve_shift_pattern_mode(small_children, small_requirements, small_staff) -> None:
    """pattern_mode=True 指定時に解の全勤務ブロックが指定パターンのいずれかに完全一致すること。"""
    patterns = (
        ShiftPattern("early", "前半", time(9, 0), time(12, 0), symbol="前"),
        ShiftPattern("late", "後半", time(11, 0), time(14, 0), symbol="後"),
        ShiftPattern("full", "通し", time(9, 0), time(14, 0), symbol="通"),
    )

    result = solve_shift(
        small_children,
        small_staff,
        small_requirements,
        time_limit_sec=10,
        patterns=patterns,
        pattern_mode=True,
    )
    assert result.ok is True
    assert result.stats.get("pattern_mode") is True

    # 得られた解の全勤務ブロックが patterns のいずれかに厳密に一致していることを検証
    slots = list(small_requirements.slots)
    days = [small_requirements.all_days()[0]]
    for st in small_staff:
        for day in days:
            shift_day = result.day(day)
            if shift_day is None:
                continue
            work_slots = [
                sl for sl in slots
                if shift_day.get(st.staff_id, sl) in (CellState.WORK, CellState.BREAK)
            ]
            if not work_slots:
                continue
            start_m = min(s.start_minutes for s in work_slots)
            end_m = max(s.end_minutes for s in work_slots)
            matched = match_pattern(start_m, end_m, patterns)
            assert matched is not None, f"{st.staff_id} on {day}: {start_m//60}:{start_m%60} - {end_m//60}:{end_m%60} did not match any pattern"


def test_export_bundle_zip_contains_symbol_matrix(test_requirements, small_staff) -> None:
    """export_bundle_zip が symbol_matrix.csv と現場シフト記号シートを含むこと。"""
    from shiftai.domain import SolveResult, SolveStatus

    slots = list(test_requirements.slots)
    assignments = {}
    shift_days = [ShiftDay(day=d) for d in DAYS]
    result = SolveResult(
        status=SolveStatus.OPTIMAL,
        shift_days=shift_days,
        assignments=assignments,
    )

    zip_bytes = export_bundle_zip(
        result,
        test_requirements,
        slots,
        small_staff,
        patterns=DEFAULT_PATTERNS,
    )

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as z:
        names = z.namelist()
        assert "symbol_matrix.csv" in names
        assert "shift.xlsx" in names

        # Excel 内のシート名確認
        xlsx_data = z.read("shift.xlsx")
        wb = load_workbook(io.BytesIO(xlsx_data), read_only=True)
        assert "現場シフト記号" in wb.sheetnames
