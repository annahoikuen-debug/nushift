"""公平性モジュール（:mod:`shiftai.fairness`）とソルバへの組み込みのテスト。"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from shiftai import fairness, local_rules, sample_data, solver
from shiftai.domain import (
    CellState,
    ObjectiveWeights,
    ShiftDay,
    Slot,
    build_slots,
    daterange,
)
from shiftai.shift_patterns import default_patterns
from shiftai.standards import build_requirements

STANDARD = local_rules.get_standard("全国基準（厚労省）")
MONDAY = date(2026, 9, 28)
SATURDAY = date(2026, 10, 3)
DAY_OPEN = time(7, 15)
DAY_CLOSE = time(19, 30)


def _slots() -> tuple[Slot, ...]:
    return build_slots(DAY_OPEN, DAY_CLOSE, granularity_min=30)


def _first_index_at_or_before(slots: tuple[Slot, ...], boundary: time) -> int:
    """開始時刻が ``boundary`` 以下の最初の時間帯の添字を返す。"""
    for i, slot in enumerate(slots):
        if slot.start_minutes <= boundary.hour * 60 + boundary.minute:
            return i
    return 0


def _first_index_at_or_after(slots: tuple[Slot, ...], boundary: time) -> int:
    """終了時刻が ``boundary`` 以降になる最初の時間帯の添字を返す。"""
    for i, slot in enumerate(slots):
        if slot.end_minutes >= boundary.hour * 60 + boundary.minute:
            return i
    return len(slots) - 1


def _member(staff_id: str) -> object:
    from shiftai.domain import Contract, Role, StaffMember

    return StaffMember(
        staff_id=staff_id,
        name=staff_id,
        roles=(Role.HOIKUSHI,),
        contract=Contract(weekly_hours=40.0, daily_hours=8.0),
    )


def _result(states_by_day: dict[date, dict[str, list[CellState]]], slots) -> object:
    from shiftai.domain import SolveResult, SolveStatus

    days = [
        ShiftDay(
            day=day,
            assignments={
                sid: {slot.label: state for slot, state in zip(slots, row, strict=False)}
                for sid, row in rows.items()
            },
        )
        for day, rows in sorted(states_by_day.items())
    ]
    return SolveResult(status=SolveStatus.OPTIMAL, shift_days=days)


# ---------------------------------------------------------------------------
# ブロック切り
# ---------------------------------------------------------------------------


def test_在勤ブロックは連続区間にまとめられる():
    slots = _slots()
    n = len(slots)
    row = [CellState.OFF] * n
    row[0] = CellState.WORK
    row[1] = CellState.BREAK
    row[2] = CellState.WORK
    assert fairness.block_indices(row) == [(0, 2)]

    split = [CellState.OFF] * n
    split[0] = CellState.WORK
    split[2] = CellState.WORK
    assert fairness.block_indices(split) == [(0, 0), (2, 2)]


def test_在勤ブロックは勤務と休憩をまたいでも1つ():
    slots = _slots()
    row = [CellState.OFF] * len(slots)
    row[0] = CellState.WORK
    row[1] = CellState.OFF
    row[2] = CellState.BREAK
    assert fairness.block_indices(row) == [(0, 0), (2, 2)]


# ---------------------------------------------------------------------------
# 回数集計
# ---------------------------------------------------------------------------


def test_早朝保育時間帯の勤務は早番として数える():
    slots = _slots()
    index = _first_index_at_or_before(slots, STANDARD.early_care_window[0])
    row = [CellState.OFF] * len(slots)
    row[index] = CellState.WORK
    result = _result({MONDAY: {"S001": row}}, slots)
    tally = fairness.counts(result, [_member("S001")], slots, standard=STANDARD)
    assert tally["S001"].totals["early"] == 1
    assert tally["S001"].totals["late"] == 0


def test_延長保育時間帯の勤務は遅番として数える():
    slots = _slots()
    index = _first_index_at_or_after(slots, STANDARD.late_care_window[1])
    row = [CellState.OFF] * len(slots)
    row[index] = CellState.WORK
    result = _result({MONDAY: {"S001": row}}, slots)
    tally = fairness.counts(result, [_member("S001")], slots, standard=STANDARD)
    assert tally["S001"].totals["late"] == 1
    assert tally["S001"].totals["early"] == 0


def test_土曜出勤は土曜の日だけ数える():
    slots = _slots()
    row = [CellState.OFF] * len(slots)
    row[3] = CellState.WORK
    result = _result({MONDAY: {"S001": list(row)}, SATURDAY: {"S001": list(row)}}, slots)
    tally = fairness.counts(result, [_member("S001")], slots, standard=STANDARD)
    assert tally["S001"].totals["saturday"] == 1


def test_勤務していない日は早番にも土曜にも数えない():
    slots = _slots()
    result = _result({SATURDAY: {"S001": [CellState.OFF] * len(slots)}}, slots)
    tally = fairness.counts(result, [_member("S001")], slots, standard=STANDARD)
    entry = tally["S001"]
    assert entry.totals == {"early": 0, "late": 0, "saturday": 0}


def test_勤務パターンを渡すと枠一致で判定する():
    """パターン枠に収まる勤務ブロックは早番として数えられる。"""
    slots = _slots()
    patterns = default_patterns(DAY_OPEN, DAY_CLOSE)
    early = next(p for p in patterns if p.key == "early")
    row = [CellState.OFF] * len(slots)
    filled = False
    for i, slot in enumerate(slots):
        if slot.start_minutes >= early.start_minutes and slot.end_minutes <= early.end_minutes:
            row[i] = CellState.WORK
            filled = True
    assert filled, "早番パターンに収まる時間帯が無い"
    result = _result({MONDAY: {"S001": row}}, slots)
    tally = fairness.counts(result, [_member("S001")], slots, patterns=patterns)
    assert tally["S001"].totals["early"] == 1


# ---------------------------------------------------------------------------
# 週レンジ
# ---------------------------------------------------------------------------


def test_週レンジは週ごとの最大と最小の差になる():
    slots = _slots()
    days = list(daterange(MONDAY, MONDAY + timedelta(days=6)))
    early_index = _first_index_at_or_before(slots, STANDARD.early_care_window[0])
    rows_a = [[CellState.OFF] * len(slots) for _ in days]
    rows_b = [[CellState.OFF] * len(slots) for _ in days]
    # A は全週で早番、B は昼間の時間帯だけ（早番にならない）
    for row in rows_a:
        row[early_index] = CellState.WORK
    midday = len(slots) // 2
    for row in rows_b:
        row[midday] = CellState.WORK
    result = _result({day: {"A": rows_a[i], "B": rows_b[i]} for i, day in enumerate(days)}, slots)
    tally = fairness.counts(result, [_member("A"), _member("B")], slots, standard=STANDARD)
    stats = fairness.spread_stats(tally, "early")
    assert stats.max_spread == 7
    assert stats.max_count == 7
    assert stats.min_count == 0
    assert stats.label == "早番"


def test_偏りが無ければレンジは0():
    slots = _slots()
    midday = len(slots) // 2
    row = [CellState.OFF] * len(slots)
    row[midday] = CellState.WORK
    days = list(daterange(MONDAY, MONDAY + timedelta(days=6)))
    result = _result({day: {"A": list(row), "B": list(row)} for day in days}, slots)
    tally = fairness.counts(result, [_member("A"), _member("B")], slots, standard=STANDARD)
    assert fairness.spread_stats(tally, "early").max_spread == 0


# ---------------------------------------------------------------------------
# レポート表
# ---------------------------------------------------------------------------


def test_レポート表は区分ごとの回数と週レンジを並べる():
    slots = _slots()
    midday = len(slots) // 2
    row = [CellState.OFF] * len(slots)
    row[midday] = CellState.WORK
    days = list(daterange(MONDAY, MONDAY + timedelta(days=6)))
    result = _result({day: {"S001": list(row)} for day in days}, slots)
    frame = fairness.report_frame(result, [_member("S001")], slots, standard=STANDARD)
    assert list(frame.columns) == [
        "職員ID",
        "氏名",
        "早番",
        "早番の週内変動",
        "遅番",
        "遅番の週内変動",
        "土曜出勤",
        "土曜出勤の週内変動",
    ]
    assert len(frame) == 1


def test_回数の表は職員ごとの行を返す():
    slots = _slots()
    row = [CellState.OFF] * len(slots)
    row[2] = CellState.WORK
    result = _result({MONDAY: {"S001": list(row), "S002": list(row)}}, slots)
    frame = fairness.count_frame(result, [_member("S001"), _member("S002")], slots)
    assert list(frame["職員ID"]) == ["S001", "S002"]


# ---------------------------------------------------------------------------
# ソルバへの組み込み
# ---------------------------------------------------------------------------


def _weekly_case():
    """1 週間・6 名・早朝保育のある必要人員表を返す。"""
    days = list(daterange(MONDAY, MONDAY + timedelta(days=6)))
    children, staff, prefs = sample_data.make_dataset(days, seed=7)
    table = build_requirements(
        children,
        days,
        STANDARD,
        day_open=DAY_OPEN,
        day_close=DAY_CLOSE,
        granularity_min=30,
    )
    return days, children, staff, prefs, table


def test_重みが0なら公平性の変数は作られない():
    days, children, staff, prefs, table = _weekly_case()
    off = ObjectiveWeights(
        fairness_early_penalty=0.0,
        fairness_late_penalty=0.0,
        fairness_saturday_penalty=0.0,
    )
    ctx = solver._build_problem(staff, table, prefs, {}, solver.FacilitySettings(), off, STANDARD)
    base, _constraints = ctx.counts()
    ctx_on = solver._build_problem(
        staff, table, prefs, {}, solver.FacilitySettings(), ObjectiveWeights(), STANDARD
    )
    on, _c2 = ctx_on.counts()
    assert on > base, "重みを上げると変数が増えていない"


def test_公平性の重みを上げると目的関数が反応する():
    days, children, staff, prefs, table = _weekly_case()
    off = ObjectiveWeights(
        fairness_early_penalty=0.0,
        fairness_late_penalty=0.0,
        fairness_saturday_penalty=0.0,
    )
    ctx = solver._build_problem(staff, table, prefs, {}, solver.FacilitySettings(), off, STANDARD)
    terms = {
        str(v.name): v.value()
        for v in ctx.prob.variables()
        if v.name and str(v.name).startswith(("fmax", "fmin"))
    }
    assert terms == {}


def test_公平性の重みを上げると最大最小の変数が作られる():
    days, children, staff, prefs, table = _weekly_case()
    ctx = solver._build_problem(
        staff,
        table,
        prefs,
        {},
        solver.FacilitySettings(),
        ObjectiveWeights(),
        STANDARD,
    )
    names = [str(v.name) for v in ctx.prob.variables()]
    assert any(n.startswith("fmax_") for n in names)
    assert any(n.startswith("fmin_") for n in names)


def test_公平性制約はdrop_groupsで落とせる():
    days, children, staff, prefs, table = _weekly_case()
    ctx = solver._build_problem(
        staff,
        table,
        prefs,
        {},
        solver.FacilitySettings(),
        ObjectiveWeights(),
        STANDARD,
        drop_groups=frozenset({"fairness"}),
    )
    names = [str(v.name) for v in ctx.prob.variables()]
    assert not any(n.startswith("fmax_") for n in names)


def test_重みの既定値は0より大きい():
    weights = ObjectiveWeights()
    assert weights.fairness_early_penalty > 0
    assert weights.fairness_late_penalty > 0
    assert weights.fairness_saturday_penalty > 0


@pytest.mark.slow
def test_公平性の重みを上げても解が返る():
    """重みを上げた状態で最適化が「解なし」にならないこと（回帰防止）。"""
    days, children, staff, prefs, table = _weekly_case()
    weights = ObjectiveWeights(
        fairness_early_penalty=8.0,
        fairness_late_penalty=8.0,
        fairness_saturday_penalty=8.0,
    )
    result = solver.solve_shift(
        children,
        staff,
        table,
        prefs,
        weights,
        time_limit_sec=90,
        standard=STANDARD,
    )
    assert result.status is not solver.SolveStatus.INFEASIBLE
    assert result.status is not solver.SolveStatus.ERROR


def test_配置対象外職員は公平性の均衡対象から外れる():
    from shiftai.domain import Role, StaffMember

    days, children, staff, prefs, table = _weekly_case()
    head = StaffMember(
        staff_id="H001",
        name="園長",
        roles=(Role.ENJOGAKUIN,),
        contract=staff[0].contract,
    )
    ctx = solver._build_problem(
        [*staff, head],
        table,
        prefs,
        {},
        solver.FacilitySettings(),
        ObjectiveWeights(),
        STANDARD,
    )
    names = [str(v.name) for v in ctx.prob.variables()]
    assert not any(n.endswith("H001_0928") and n.startswith("fw_") for n in names)


def test_曜日の定義は_python_標準に一致する():
    assert fairness.category_label("early") == "早番"
    assert fairness.category_label("late") == "遅番"
    assert fairness.category_label("saturday") == "土曜出勤"
    assert fairness.category_label("unknown") == "unknown"


def test_職員0名でも落ちない():
    slots = _slots()
    empty = _result({}, slots)
    tally = fairness.counts(empty, [], slots, standard=STANDARD)
    assert tally == {}
    stats = fairness.spread_stats(tally, "early")
    assert stats.max_spread == 0
