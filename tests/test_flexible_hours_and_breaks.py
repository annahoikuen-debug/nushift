"""勤務3分割禁止・休憩時間45〜120分・正社員4〜8時間勤務柔軟化のテスト。"""
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
    RequirementTable,
    Role,
    Slot,
    SolveStatus,
    StaffMember,
    ViolationSeverity,
)
from shiftai.gap_analysis import check_violations
from shiftai.solver import solve_shift


def test_contract_and_facility_settings_fields():
    """新設されたフィールドとバリデーションが正常に動作すること。"""
    c = Contract(
        weekly_hours=40.0,
        daily_hours=8.0,
        employment_type=EmploymentType.SEI,
        min_daily_hours=4.0,
        break_minutes=90,
        max_work_blocks_per_day=2,
    )
    assert c.min_daily_hours == 4.0
    assert c.break_minutes == 90
    assert c.max_work_blocks_per_day == 2

    fs = FacilitySettings(
        max_work_blocks_per_day=2,
        break_minutes=75,
        sei_min_daily_hours=4.0,
        sei_max_daily_hours=8.0,
    )
    assert fs.max_work_blocks_per_day == 2
    assert fs.break_minutes == 75
    assert fs.sei_min_daily_hours == 4.0
    assert fs.sei_max_daily_hours == 8.0

    # 異常値のバリデーション
    with pytest.raises(ValueError):
        Contract(weekly_hours=40.0, daily_hours=8.0, break_minutes=30)  # 45分未満
    with pytest.raises(ValueError):
        Contract(weekly_hours=40.0, daily_hours=8.0, break_minutes=150)  # 120分超
    with pytest.raises(ValueError):
        Contract(weekly_hours=40.0, daily_hours=8.0, min_daily_hours=9.0)  # daily_hours 超


def test_gap_analysis_detects_3_split_shift_as_split_shift():
    """勤務が3分割された場合に SPLIT_SHIFT 違反が検出されること。"""
    d = date(2026, 11, 2)
    c = Contract(weekly_hours=40.0, daily_hours=8.0, employment_type=EmploymentType.SEI)
    staff = [StaffMember(staff_id="s1", name="保育士1", roles=(Role.HOIKUSHI,), contract=c)]
    slots = [
        Slot(start=time(7 + i // 2, (i % 2) * 30), end=time(7 + (i + 1) // 2, ((i + 1) % 2) * 30))
        for i in range(16)  # 7:00 - 15:00
    ]
    req = RequirementTable(
        day_open=time(7, 0),
        day_close=time(15, 0),
        granularity_min=30,
        slots=slots,
        rows={d: []},
    )

    # 3分割勤務: 勤務(0..1) -> 休み(2..3) -> 勤務(4..5) -> 休み(6..7) -> 勤務(8..9) -> 残り休み
    from shiftai.domain import ShiftDay, SolveResult
    row_s1: dict[str, CellState] = {}
    for i in range(16):
        if i in (0, 1, 4, 5, 8, 9):
            row_s1[slots[i].label] = CellState.WORK
        else:
            row_s1[slots[i].label] = CellState.OFF

    shift_day = ShiftDay(day=d, assignments={"s1": row_s1})
    result = SolveResult(status=SolveStatus.FEASIBLE, shift_days=[shift_day])

    violations = check_violations(req, result, staff)
    split_violations = [v for v in violations if v.code == "SPLIT_SHIFT"]
    assert len(split_violations) >= 1
    assert split_violations[0].detail["blocks"] == 3


def test_gap_analysis_fulltime_short_daily_hours():
    """正社員で勤務時間が4時間未満の場合に DAILY_HOURS_SHORT が検出されること。"""
    d = date(2026, 11, 2)
    c = Contract(
        weekly_hours=40.0,
        daily_hours=8.0,
        employment_type=EmploymentType.SEI,
        min_daily_hours=4.0,
    )
    staff = [StaffMember(staff_id="s1", name="保育士1", roles=(Role.HOIKUSHI,), contract=c)]
    slots = [
        Slot(start=time(8 + i // 2, (i % 2) * 30), end=time(8 + (i + 1) // 2, ((i + 1) % 2) * 30))
        for i in range(8)  # 8:00 - 12:00
    ]
    req = RequirementTable(
        day_open=time(8, 0),
        day_close=time(12, 0),
        granularity_min=30,
        slots=slots,
        rows={d: []},
    )

    # 2時間（4スロット）のみ勤務
    from shiftai.domain import ShiftDay, SolveResult
    row_s1 = {slots[i].label: CellState.WORK if i < 4 else CellState.OFF for i in range(8)}
    shift_day = ShiftDay(day=d, assignments={"s1": row_s1})
    result = SolveResult(status=SolveStatus.FEASIBLE, shift_days=[shift_day])

    violations = check_violations(req, result, staff)
    short_violations = [v for v in violations if v.code == "DAILY_HOURS_SHORT"]
    assert len(short_violations) >= 1
    assert short_violations[0].detail["hours"] == 2.0
    assert short_violations[0].detail["min_hours"] == 4.0


def test_gap_analysis_fulltime_exceeded_daily_hours():
    """正社員で施設設定の上限勤務時間（7時間）を超えた場合に DAILY_HOURS_EXCEEDED が検出されること。"""
    d = date(2026, 11, 2)
    c = Contract(
        weekly_hours=40.0,
        daily_hours=8.0,
        employment_type=EmploymentType.SEI,
    )
    staff = [StaffMember(staff_id="s1", name="保育士1", roles=(Role.HOIKUSHI,), contract=c)]
    slots = [
        Slot(start=time(8 + i // 2, (i % 2) * 30), end=time(8 + (i + 1) // 2, ((i + 1) % 2) * 30))
        for i in range(16)  # 8:00 - 16:00 (8時間)
    ]
    req = RequirementTable(
        day_open=time(8, 0),
        day_close=time(16, 0),
        granularity_min=30,
        slots=slots,
        rows={d: []},
    )

    # 7.5時間（15スロット）勤務
    from shiftai.domain import ShiftDay, SolveResult
    row_s1 = {slots[i].label: CellState.WORK if i < 15 else CellState.OFF for i in range(16)}
    shift_day = ShiftDay(day=d, assignments={"s1": row_s1})
    result = SolveResult(status=SolveStatus.FEASIBLE, shift_days=[shift_day])

    settings = FacilitySettings(sei_max_daily_hours=7.0)
    violations = check_violations(req, result, staff, settings=settings)
    exceeded_violations = [v for v in violations if v.code == "DAILY_HOURS_EXCEEDED"]
    assert len(exceeded_violations) >= 1
    assert exceeded_violations[0].detail["hours"] == 7.5
    assert exceeded_violations[0].detail["max_hours"] == 7.0


def test_solver_respects_break_minutes_and_fulltime_hours():
    """ソルバーが正社員4〜8時間勤務および個別休憩時間（90分）を満たして求解すること。"""
    d = date(2026, 11, 2)
    c = Contract(
        weekly_hours=40.0,
        daily_hours=8.0,
        employment_type=EmploymentType.SEI,
        min_daily_hours=4.0,
        break_minutes=90,  # 90分休憩 = 3スロット (30分粒度)
    )
    staff = [StaffMember(staff_id="s1", name="保育士1", roles=(Role.HOIKUSHI,), contract=c)]
    slots = [
        Slot(start=time(7 + i // 2, (i % 2) * 30), end=time(7 + (i + 1) // 2, ((i + 1) % 2) * 30))
        for i in range(20)  # 7:00 - 17:00
    ]
    from shiftai.domain import AgeClass, Requirement
    req_row = [
        Requirement(
            day=d,
            slot=s,
            age_class=AgeClass.INFANT,
            child_count=1,
            needed_staff=1,
            needed_qualified=1,
            basis="テスト",
        )
        for s in slots[2:16]  # 8:00 - 15:00 需要
    ]
    req = RequirementTable(
        day_open=time(7, 0),
        day_close=time(17, 0),
        granularity_min=30,
        slots=slots,
        rows={d: req_row},
    )

    res = solve_shift(
        children=[],
        staff=staff,
        requirements=req,
        time_limit_sec=10,
    )
    assert res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    day_res = res.shift_days[0]
    work_count = sum(1 for s in slots if day_res.get("s1", s) is CellState.WORK)
    break_count = sum(1 for s in slots if day_res.get("s1", s) is CellState.BREAK)

    # 休憩スロットが 90分（3スロット以上）割り当てられていること
    assert break_count >= 3
    # 勤務時間は 4時間（8スロット）以上 8時間（16スロット）以下であること
    assert 8 <= work_count <= 16


def test_facility_settings_applied_to_solver():
    """職員個別の指定がない場合、FacilitySettings の値（休憩90分、正社員5〜7時間）が適用されること。"""
    d = date(2026, 11, 2)
    c = Contract(
        weekly_hours=40.0,
        daily_hours=8.0,
        employment_type=EmploymentType.SEI,
    )
    staff = [StaffMember(staff_id="s1", name="保育士1", roles=(Role.HOIKUSHI,), contract=c)]
    slots = [
        Slot(start=time(7 + i // 2, (i % 2) * 30), end=time(7 + (i + 1) // 2, ((i + 1) % 2) * 30))
        for i in range(20)  # 7:00 - 17:00
    ]
    from shiftai.domain import AgeClass, Requirement
    req_row = [
        Requirement(
            day=d,
            slot=s,
            age_class=AgeClass.INFANT,
            child_count=1,
            needed_staff=1,
            needed_qualified=1,
            basis="テスト",
        )
        for s in slots[2:16]  # 8:00 - 15:00 需要
    ]
    req = RequirementTable(
        day_open=time(7, 0),
        day_close=time(17, 0),
        granularity_min=30,
        slots=slots,
        rows={d: req_row},
    )

    settings = FacilitySettings(
        break_minutes=90,
        sei_min_daily_hours=5.0,
        sei_max_daily_hours=7.0,
        max_work_blocks_per_day=2,
    )

    res = solve_shift(
        children=[],
        staff=staff,
        requirements=req,
        settings=settings,
        time_limit_sec=10,
    )
    assert res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    day_res = res.shift_days[0]
    work_count = sum(1 for s in slots if day_res.get("s1", s) is CellState.WORK)
    break_count = sum(1 for s in slots if day_res.get("s1", s) is CellState.BREAK)

    # 休憩スロットが 90分（3スロット以上）
    assert break_count >= 3
    # 勤務時間は 5.0時間（10スロット）〜 7.0時間（14スロット）
    assert 10 <= work_count <= 14


def test_validation_out_of_range():
    """許容範囲外の指定で ValueError が送出されること。"""
    # Contract: 休憩 45-120 分
    with pytest.raises(ValueError):
        Contract(weekly_hours=40.0, daily_hours=8.0, break_minutes=44)
    with pytest.raises(ValueError):
        Contract(weekly_hours=40.0, daily_hours=8.0, break_minutes=125)

    # Contract: 正社員最低 4.0h 以上
    with pytest.raises(ValueError):
        Contract(
            weekly_hours=40.0,
            daily_hours=8.0,
            employment_type=EmploymentType.SEI,
            min_daily_hours=3.5,
        )

    # FacilitySettings: 休憩 45-120 分
    with pytest.raises(ValueError):
        FacilitySettings(break_minutes=40)
    with pytest.raises(ValueError):
        FacilitySettings(break_minutes=130)

    # FacilitySettings: 正社員勤務時間 4.0h〜8.0h
    with pytest.raises(ValueError):
        FacilitySettings(sei_min_daily_hours=3.0)
    with pytest.raises(ValueError):
        FacilitySettings(sei_max_daily_hours=8.5)
    with pytest.raises(ValueError):
        FacilitySettings(sei_min_daily_hours=7.0, sei_max_daily_hours=6.0)

    # FacilitySettings: 最大ブロック数 1〜4
    with pytest.raises(ValueError):
        FacilitySettings(max_work_blocks_per_day=0)
    with pytest.raises(ValueError):
        FacilitySettings(max_work_blocks_per_day=5)
