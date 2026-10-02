"""``shiftai.ui.gantt`` のテスト。

日別ガントの表示はブラウザに描かれるため、ここでは **HTML 文字列**だけを
検証する。Streamlit 実行コンテキストが無くても ``gantt_html`` は呼べるので、
配置（``left`` / ``width``）とエスケープを数値と文字列で固定する。
"""

from __future__ import annotations

import logging
from datetime import date, time
from pathlib import Path
from typing import Any

import pytest

from shiftai.domain import CellState, ShiftDay, Slot, StaffMember
from shiftai.ui import gantt

logging.getLogger("streamlit").setLevel(logging.ERROR)

pytestmark = pytest.mark.timeout(300)

ROOT = Path(__file__).resolve().parent.parent

app_test = pytest.importorskip("streamlit.testing.v1", reason="AppTest が無い環境ではスキップ")


def _member(staff_id: str = "S001", name: str = "保育士1") -> StaffMember:
    """``StaffMember`` 1 人を作る（``name`` だけ差し替え可能）。"""
    from shiftai.domain import Contract, Role

    return StaffMember(
        staff_id=staff_id,
        name=name,
        roles=(Role.HOIKUSHI,),
        contract=Contract(weekly_hours=40.0, daily_hours=8.0),
    )


def _shift_day(states: dict[str, dict[str, CellState]], day: Any) -> ShiftDay:
    """``assignments`` から ``ShiftDay`` を作る。"""
    return ShiftDay(day=day, assignments={key: dict(value) for key, value in states.items()})


# --- 時刻と時間軸 -----------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, "00:00"), (450, "07:30"), (1439, "23:59"), (1440, "24:00")],
)
def test_clock_textは分をHH_MMにする(minutes: int, expected: str) -> None:
    assert gantt.clock_text(minutes) == expected


def test_axis_rangeは開所から閉所まで() -> None:
    slots = (Slot(time(7, 0), time(7, 30)), Slot(time(23, 30), time(0, 0)))
    assert gantt.axis_range(slots) == (420, 1440)


def test_axis_rangeは時間帯無しで0() -> None:
    assert gantt.axis_range(()) == (0, 0)


def test_axis_ticksは両端を含む() -> None:
    ticks = gantt.axis_ticks(540, 720)
    assert ticks[0] == (540, "09:00")
    assert ticks[-1] == (720, "12:00")
    assert (600, "10:00") in ticks


def test_axis_ticksは幅が広いと間引く() -> None:
    """1 日幅では 1 時間刻みだと目盛が重なるので 2 時間刻みにすること。"""
    labels = [label for _, label in gantt.axis_ticks(0, 1440)]
    assert labels[0] == "00:00"
    assert labels[-1] == "24:00"
    assert labels == [
        "00:00",
        "02:00",
        "04:00",
        "06:00",
        "08:00",
        "10:00",
        "12:00",
        "14:00",
        "16:00",
        "18:00",
        "20:00",
        "22:00",
        "24:00",
    ]


def test_axis_ticksは幅0で空() -> None:
    assert gantt.axis_ticks(600, 600) == []


# --- ブロックの切り出し -----------------------------------------------------


def test_連続する勤務は1本のブロックになる(slots, one_day) -> None:
    """勤務が連続していれば、時間帯ごとではなく 1 本の横棒になること。"""
    day = one_day[0]
    member = _member()
    worked = [s for s in slots if 9 * 60 <= s.start_minutes < 12 * 60]
    shift = _shift_day({member.staff_id: {s.label: CellState.WORK for s in worked}}, day)
    rows = gantt.gantt_rows(shift, slots, [member])
    assert len(rows) == 1
    bars = rows[0].bars
    assert len(bars) == 1
    assert bars[0].start_minutes == 9 * 60
    assert bars[0].end_minutes == 12 * 60
    assert bars[0].hours == pytest.approx(3.0)


def test_勤務と休憩は別々のブロックになる(slots, one_day) -> None:
    day = one_day[0]
    member = _member()
    work = [s for s in slots if 9 * 60 <= s.start_minutes < 12 * 60]
    rest = [s for s in slots if 10 * 60 <= s.start_minutes < 11 * 60]
    cells = {s.label: CellState.WORK for s in work}
    cells.update({s.label: CellState.BREAK for s in rest})
    rows = gantt.gantt_rows(_shift_day({member.staff_id: cells}, day), slots, [member])
    bars = rows[0].bars
    assert [bar.state for bar in bars] == [CellState.WORK, CellState.BREAK, CellState.WORK]
    assert rows[0].work_minutes == 120
    assert rows[0].break_minutes == 60
    assert rows[0].label == "S001 保育士1"


def test_勤務なしは横棒を持たない(slots, one_day) -> None:
    day = one_day[0]
    member = _member()
    rows = gantt.gantt_rows(_shift_day({}, day), slots, [member])
    assert rows[0].bars == ()
    assert rows[0].work_minutes == 0


def test_シフト無しでも職員ごとの行は並ぶ(slots) -> None:
    """``ShiftDay`` が ``None`` でも「勤務なし」の行を職員人数分出すこと。"""
    members = [_member("S001"), _member("S002")]
    rows = gantt.gantt_rows(None, slots, members)
    assert [row.staff_id for row in rows] == ["S001", "S002"]
    assert all(row.bars == () for row in rows)


def test_確定セルはブロックがロック表示になる(slots, one_day) -> None:
    day = one_day[0]
    member = _member()
    worked = [s for s in slots if 9 * 60 <= s.start_minutes < 12 * 60]
    shift = _shift_day({member.staff_id: {s.label: CellState.WORK for s in worked}}, day)
    fixed = {(member.staff_id, day, worked[1].label): CellState.WORK}
    locked = gantt.gantt_rows(shift, slots, [member], day=day, fixed=fixed)[0]
    assert locked.bars[0].locked is True
    plain = gantt.gantt_rows(shift, slots, [member])[0]
    assert plain.bars[0].locked is False


def test_24時表記の時間帯でも折り返さない() -> None:
    """深夜に閉所する園では最後の時間帯が 24:00 表記になる。"""
    slots = (Slot(time(23, 0), time(23, 30)), Slot(time(23, 30), time(0, 0)))
    member = _member()
    shift = _shift_day(
        {member.staff_id: {s.label: CellState.WORK for s in slots}}, date(2026, 10, 1)
    )
    row = gantt.gantt_rows(shift, slots, [member])[0]
    assert len(row.bars) == 1
    assert row.bars[0].end_minutes == 1440
    html = gantt.gantt_html(shift, slots, [member])
    assert "width:100.0%" in html


# --- HTML ------------------------------------------------------------------


def test_HTMLは職員ごとの行と目盛りを持つ(slots, one_day) -> None:
    day = one_day[0]
    member = _member()
    worked = [s for s in slots if 9 * 60 <= s.start_minutes < 12 * 60]
    shift = _shift_day({member.staff_id: {s.label: CellState.WORK for s in worked}}, day)
    html = gantt.gantt_html(shift, slots, [member], day=day)
    assert html.count("shiftai-gantt-row") == 1
    assert "shiftai-gantt-bar--work" in html
    assert "3.0h" in html
    assert "09:00" in html


def test_生成したHTMLに改行を入れない(slots, one_day) -> None:
    """Streamlit の Markdown は連続行を段落に分けるため、HTML は 1 行に畳むこと。"""
    html = gantt.gantt_html(_shift_day({}, one_day[0]), slots, [_member()])
    assert "\n" not in html


def test_氏名のマークアップはエスケープする(slots, one_day) -> None:
    """``unsafe_allow_html=True`` はサーバー側でサニタイズしないため。"""
    member = _member(name='<img src=x onerror="alert(1)">')
    html = gantt.gantt_html(_shift_day({}, one_day[0]), slots, [member])
    assert "<img" not in html
    assert "&lt;img" in html


def test_勤務なしの行にはその旨が出る(slots, one_day) -> None:
    html = gantt.gantt_html(_shift_day({}, one_day[0]), slots, [_member()])
    assert "shiftai-gantt-empty" in html
    assert "勤務なし" in html


def test_休憩ブロックは斜線スタイルで描く(slots, one_day) -> None:
    day = one_day[0]
    member = _member()
    rest = [s for s in slots if 11 * 60 <= s.start_minutes < 12 * 60]
    shift = _shift_day({member.staff_id: {s.label: CellState.BREAK for s in rest}}, day)
    html = gantt.gantt_html(shift, slots, [member], day=day)
    assert "shiftai-gantt-bar--break" in html


def test_空の時間帯でも壊さない() -> None:
    assert gantt.gantt_html(None, (), []) != ""
    assert "shiftai-gantt" in gantt.gantt_html(None, (), [])


# --- render（Streamlit 実行コンテキスト） ----------------------------------

_DRIVER = """
import sys
sys.path.insert(0, {src!r})
from datetime import time
import streamlit as st
from shiftai.domain import CellState, ShiftDay, Slot, StaffMember, Contract, Role
from shiftai.ui import gantt, theme
theme.apply_page_config()
slots = (Slot(time(8, 0), time(8, 30)), Slot(time(8, 30), time(9, 0)),
         Slot(time(9, 0), time(9, 30)), Slot(time(9, 30), time(10, 0)))
member = StaffMember(staff_id="S001", name="保育士1", roles=(Role.HOIKUSHI,),
                     contract=Contract(weekly_hours=40.0, daily_hours=8.0))
shift = ShiftDay(day=__import__("datetime").date(2026, 10, 1),
                 assignments={{"S001": {{"08:00-08:30": CellState.WORK,
                                        "08:30-09:00": CellState.WORK,
                                        "09:00-09:30": CellState.WORK,
                                        "09:30-10:00": CellState.WORK}}}})
gantt.render(shift, slots, [member])
gantt.render(None, slots, [member])
"""


def _run_render(tmp_path: Path) -> Any:
    script = tmp_path / "gantt_render.py"
    script.write_text(_DRIVER.format(src=str(ROOT / "src")), encoding="utf-8")
    at = app_test.AppTest.from_file(str(script), default_timeout=120)
    at.run()
    return at


def test_renderはシフト無しで案内を出す(tmp_path) -> None:
    at = _run_render(tmp_path)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("この日のシフトがありません" in info.value for info in at.info)


def test_renderはガントのHTMLを出す(tmp_path) -> None:
    at = _run_render(tmp_path)
    assert not at.exception, [str(e.value) for e in at.exception]
    html = "".join(m.value for m in at.markdown)
    assert "shiftai-gantt-track" in html


# --- CSS -------------------------------------------------------------------


def test_ガントのクラス名がCSSに定義されている() -> None:
    from shiftai.ui import theme

    for css_class in (
        "shiftai-gantt-scroll",
        "shiftai-gantt-axis",
        "shiftai-gantt-tick",
        "shiftai-gantt-track",
        "shiftai-gantt-bar--work",
        "shiftai-gantt-bar--break",
        "shiftai-gantt-bar--locked",
        "shiftai-gantt-empty",
    ):
        assert css_class in theme.CSS, f"{css_class} の CSS が無い"
