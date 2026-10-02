"""即時バリデーション（:mod:`shiftai.live_validation`）のテスト。"""

from __future__ import annotations

from datetime import date, time

import pandas as pd

from shiftai import live_validation
from shiftai.domain import (
    CellState,
    ChildPlan,
    Contract,
    FacilitySettings,
    Role,
    Slot,
    StaffMember,
    StaffPreferences,
    Unavailability,
    build_slots,
)
from shiftai.standards import build_requirements

DAY = date(2026, 9, 28)
SATURDAY = date(2026, 10, 3)
DAY_OPEN = time(9, 0)
DAY_CLOSE = time(14, 0)


def _slots() -> tuple[Slot, ...]:
    return build_slots(DAY_OPEN, DAY_CLOSE, granularity_min=30)


def _staff(
    staff_id: str = "S001", *, earliest: time = time(9, 0), latest: time = time(14, 0)
) -> StaffMember:
    return StaffMember(
        staff_id=staff_id,
        name="太郎",
        roles=(Role.HOIKUSHI,),
        contract=Contract(
            weekly_hours=40.0,
            daily_hours=4.0,
            earliest_start=earliest,
            latest_end=latest,
        ),
    )


def _table(children: list[ChildPlan], day: date = DAY):
    from shiftai import local_rules

    standard = local_rules.get_standard("全国基準（厚労省）")
    return (
        build_requirements(
            children,
            [day],
            standard,
            day_open=DAY_OPEN,
            day_close=DAY_CLOSE,
            granularity_min=30,
        ),
        standard,
    )


def _children(count: int = 3, day: date = DAY) -> list[ChildPlan]:
    from shiftai.domain import AgeClass

    return [
        ChildPlan(
            child_id=f"C{i:03d}",
            name=f"園児{i}",
            day=day,
            age_class=AgeClass.INFANT,
            arrive=time(9, 0),
            depart=time(14, 0),
        )
        for i in range(count)
    ]


def _row(states: list[CellState], slots) -> dict[str, CellState]:
    return {slot.label: state for slot, state in zip(slots, states, strict=False)}


def _codes(report: live_validation.LiveReport) -> set[str]:
    return {i.code for i in report.issues}


# ---------------------------------------------------------------------------
# 正常系
# ---------------------------------------------------------------------------


def test_基準を満たすシフトは指摘を返さない():
    slots = _slots()
    staff = [_staff(f"S{i:03d}") for i in range(1, 4)]
    table, standard = _table(_children(3))
    grid = {m.staff_id: [CellState.WORK] * len(slots) for m in staff}
    report = live_validation.validate_day(DAY, table, staff, slots, grid, standard=standard)
    assert report.has_errors is False
    assert "SHORTFALL_STAFF" not in _codes(report)


# ---------------------------------------------------------------------------
# 配置基準の不足
# ---------------------------------------------------------------------------


def test_人数が減ると配置不足でエラーになる():
    """0歳児 12 名（必要 4 名）に 4 名で充当し、1 人休ませると不足になること。"""
    slots = _slots()
    staff = [_staff(f"S{i:03d}") for i in range(1, 5)]
    table, standard = _table(_children(12))
    grid = {m.staff_id: [CellState.WORK] * len(slots) for m in staff}
    before = live_validation.validate_day(DAY, table, staff, slots, grid, standard=standard)
    assert "SHORTFALL_STAFF" not in _codes(before)
    grid["S004"] = [CellState.OFF] * len(slots)
    after = live_validation.validate_day(DAY, table, staff, slots, grid, standard=standard)
    assert after.has_errors
    assert "SHORTFALL_STAFF" in _codes(after)
    assert after.error_columns()


def test_保育士が減ると保育士不足でエラーになる():
    slots = _slots()
    staff = [_staff("S001"), _staff("S002")]
    child = _children(1)[0]
    table, standard = _table([child])
    grid = {"S001": [CellState.WORK] * len(slots)}
    grid["S002"] = [CellState.OFF] * len(slots)
    report = live_validation.validate_day(DAY, table, staff, slots, grid, standard=standard)
    assert "SHORTFALL_STAFF" in _codes(report)


# ---------------------------------------------------------------------------
# 契約と希望
# ---------------------------------------------------------------------------


def test_契約時間帯の外へ勤務するとエラーになる():
    slots = _slots()
    staff = [_staff("S001", earliest=time(10, 0), latest=time(14, 0))]
    table, standard = _table(_children(1))
    grid = {"S001": [CellState.WORK] * len(slots)}
    report = live_validation.validate_day(DAY, table, staff, slots, grid, standard=standard)
    codes = _codes(report)
    assert "BEFORE_EARLIEST_START" in codes


def test_希望休に勤務するとエラーになる():
    slots = _slots()
    staff = [_staff("S001")]
    table, standard = _table(_children(1))
    prefs = {
        "S001": StaffPreferences(
            unavailable=[Unavailability(day=DAY, start=time(9, 0), end=time(11, 0))]
        )
    }
    grid = {"S001": [CellState.WORK] * len(slots)}
    report = live_validation.validate_day(
        DAY, table, staff, slots, grid, preferences=prefs, standard=standard
    )
    assert "UNAVAILABLE_WORK" in _codes(report)
    assert report.error_cells()


def test_休園日に勤務するとエラーになる():
    slots = _slots()
    staff = [_staff("S001")]
    table, standard = _table(_children(1))
    grid = {"S001": [CellState.WORK] * len(slots)}
    settings = FacilitySettings(closed_days=frozenset({DAY}))
    report = live_validation.validate_day(
        DAY, table, staff, slots, grid, settings=settings, standard=standard
    )
    assert "CLOSED_DAY_WORK" in _codes(report)


def test_休日不可の職員に土曜勤務させるとエラーになる():
    slots = _slots()
    member = StaffMember(
        staff_id="S001",
        name="太郎",
        roles=(Role.HOIKUSHI,),
        contract=Contract(weekly_hours=40.0, daily_hours=4.0, can_work_holiday=False),
    )
    table, standard = _table(_children(1), SATURDAY)
    grid = {"S001": [CellState.WORK] * len(slots)}
    report = live_validation.validate_day(SATURDAY, table, [member], slots, grid, standard=standard)
    assert "HOLIDAY_NOT_ALLOWED" in _codes(report)


# ---------------------------------------------------------------------------
# 労働基準
# ---------------------------------------------------------------------------


def test_最低休憩時間を満たさないと警告になる():
    """6 時間超勤務（休憩なし）で、法令上の最低休憩時間 45 分に満たないこと。"""
    from shiftai import local_rules
    from shiftai.standards import build_requirements

    slots = build_slots(time(9, 0), time(16, 0), granularity_min=30)
    staff = [_staff("S001", earliest=time(9, 0), latest=time(16, 0))]
    standard = local_rules.get_standard("全国基準（厚労省）")
    table = build_requirements(
        _children(1),
        [DAY],
        standard,
        day_open=time(9, 0),
        day_close=time(16, 0),
        granularity_min=30,
    )
    grid = {"S001": [CellState.WORK] * len(slots)}
    report = live_validation.validate_day(DAY, table, staff, slots, grid, standard=standard)
    assert "BREAK_INSUFFICIENT" in _codes(report)
    assert not any(i.is_error and i.code == "BREAK_INSUFFICIENT" for i in report.issues)


def test_勤務が分断されると警告になる():
    slots = _slots()
    staff = [_staff("S001")]
    table, standard = _table(_children(1))
    states = [CellState.WORK] * len(slots)
    states[2] = CellState.OFF
    grid = {"S001": states}
    report = live_validation.validate_day(DAY, table, staff, slots, grid, standard=standard)
    assert "DUTY_DISCONTIGUOUS" in _codes(report)


def test_1日の契約時間を超えると警告になる():
    slots = _slots()
    staff = [_staff("S001")]
    table, standard = _table(_children(1))
    grid = {"S001": [CellState.WORK] * len(slots)}
    report = live_validation.validate_day(DAY, table, staff, slots, grid, standard=standard)
    codes = _codes(report)
    assert "DAILY_CONTRACT_EXCEEDED" in codes or "DAILY_CAP_EXCEEDED" in codes


# ---------------------------------------------------------------------------
# 入力を解釈する
# ---------------------------------------------------------------------------


def test_DataFrameのindexが職員IDでも読める():
    slots = _slots()
    staff = [_staff("S001")]
    frame = pd.DataFrame(
        [[CellState.WORK.value] * len(slots)],
        index=["S001"],
        columns=[s.label for s in slots],
    )
    states = live_validation.states_from_grid(frame, staff, slots)
    assert states["S001"][0] is CellState.WORK


def test_DataFrameのindexが職員IDと氏名でも読める():
    slots = _slots()
    staff = [_staff("S001")]
    frame = pd.DataFrame(
        [[CellState.WORK.value] * len(slots)],
        index=["S001 太郎"],
        columns=[s.label for s in slots],
    )
    states = live_validation.states_from_grid(frame, staff, slots)
    assert len(states["S001"]) == len(slots)


def test_解釈できない値はオフとして扱う():
    slots = _slots()
    staff = [_staff("S001")]
    frame = pd.DataFrame([["不明"] * len(slots)], index=["S001"], columns=[s.label for s in slots])
    states = live_validation.states_from_grid(frame, staff, slots)
    assert all(s is CellState.OFF for s in states["S001"])


def test_辞書の行も読める():
    slots = _slots()
    staff = [_staff("S001")]
    grid = {"S001": _row([CellState.WORK] * len(slots), slots)}
    states = live_validation.states_from_grid(grid, staff, slots)
    assert all(s is CellState.WORK for s in states["S001"])


def test_空表でも落ちない():
    slots = _slots()
    staff = [_staff("S001")]
    assert live_validation.states_from_grid(pd.DataFrame(), staff, slots)["S001"] == [
        CellState.OFF
    ] * len(slots)


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------


def test_サマリーと表が埋まる():
    slots = _slots()
    staff = [_staff("S001")]
    table, standard = _table(_children(3))
    grid = {"S001": [CellState.OFF] * len(slots)}
    report = live_validation.validate_day(DAY, table, staff, slots, grid, standard=standard)
    assert "エラー" in report.summary()
    frame = live_validation.to_dataframe(report)
    assert list(frame.columns) == ["深刻度", "種別", "職員ID", "時間帯", "内容"]
    assert not frame.empty


def test_指摘が無ければ空の表を返す():
    report = live_validation.LiveReport(day=DAY)
    assert live_validation.to_dataframe(report).empty
    assert report.summary() == "指摘はありません。"


def test_深刻度のラベルは日本語():
    issue = live_validation.LiveIssue("error", "X", "本文")
    assert issue.level_label == "配置基準・契約違反"
    assert issue.is_error
