"""日別シフトを「ガントチャート」風に描くウィジェット。

職員×時間帯のグリッド（:func:`shiftai.ui.components.shift_grid_frame`）は
勤務・休憩・オフが 1 セル 1 時間帯で散らばるため、勤務が連続しているか／
どこで切れているかを縦横に見るには負担が大きい。ここでは同じ情報を
「職員が 1 行・時間帯が 1 本の時間軸」という横棒（ガント）型の表示に置き換える。

``plotly`` などの追加依存は増やさない。時間軸上の位置（``left`` / ``width``）を
CSS のパーセントで指定するだけで表現でき、Streamlit の
``st.markdown(unsafe_allow_html=True)`` で描ける。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from html import escape
from typing import Any

import streamlit as st

from shiftai.domain import CellState, ShiftDay, Slot
from shiftai.ui.components import LOCK

#: 目盛ラベルを最低この間隔（%）で間引く（重なって読めなくなるため）。
TICK_MIN_GAP_PCT = 5.0
#: 目盛の間隔の上限（分）。ここまで間引いても狭いときはこの刻みに止める。
TICK_MAX_STEP = 240
#: 棒の内側に文字を出すための最小幅（%）。
BAR_TEXT_MIN_PCT = 4.0


@dataclass(frozen=True)
class GanttBar:
    """職員 1 人の連続ブロック（勤務 or 休憩）。"""

    state: CellState
    start_minutes: int
    end_minutes: int
    locked: bool = False

    @property
    def minutes(self) -> int:
        return self.end_minutes - self.start_minutes

    @property
    def hours(self) -> float:
        return self.minutes / 60.0


@dataclass(frozen=True)
class GanttRow:
    """職員 1 人の 1 日分（ブロックと集計）。"""

    staff_id: str
    name: str
    bars: tuple[GanttBar, ...] = ()
    work_minutes: int = 0
    break_minutes: int = 0

    @property
    def label(self) -> str:
        return f"{self.staff_id} {self.name}".strip()


def clock_text(minutes: int) -> str:
    """分 → ``"07:00"``。1440 は 24 時表記の ``"24:00"`` にする。"""
    total = minutes if minutes >= 24 * 60 else minutes % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def axis_range(slots: Sequence[Slot]) -> tuple[int, int]:
    """時間軸の範囲（分）。開所〜閉所に対応する。"""
    if not slots:
        return (0, 0)
    return slots[0].start_minutes, slots[-1].end_minutes


def axis_ticks(axis_start: int, axis_end: int) -> list[tuple[int, str]]:
    """時間軸の目盛り（分, ラベル）を返す。

    1 時間刻みで描き、間隔が狭すぎる場合は 2 時間・4 時間刻みに広げる。
    開所と閉所は必ず含める（軸の両端が読めなくなるため）。
    """
    span = axis_end - axis_start
    if span <= 0:
        return []
    step = 60
    while step < TICK_MAX_STEP and 100.0 * step / span < TICK_MIN_GAP_PCT:
        step += 60
    marks = [axis_start, axis_end]
    boundary = axis_start + step - (axis_start % step)
    while boundary < axis_end:
        marks.append(boundary)
        boundary += step
    # 開所・閉所を先に積んだので、時系列に並べ直してから返す。
    return [(mark, clock_text(mark)) for mark in sorted(marks)]


def gantt_rows(
    shift_day: ShiftDay | None,
    slots: Sequence[Slot],
    staff: Sequence[Any],
    *,
    day: date | None = None,
    fixed: dict[tuple[str, date, str], Any] | None = None,
) -> list[GanttRow]:
    """職員ごとの連続ブロック（勤務／休憩）を求める。

    ``fixed`` は ``(職員ID, 日付, 時間帯ラベル)`` をキーにした確定済みセルの
    マップ。確定セルの ``locked`` 表示に使う。
    """
    rows: list[GanttRow] = []
    for member in staff:
        bars: list[GanttBar] = []
        work_minutes = 0
        break_minutes = 0
        run_state: CellState | None = None
        run_start = 0
        run_end = 0
        run_locked = False
        for slot in slots:
            state = shift_day.get(member.staff_id, slot) if shift_day else CellState.OFF
            locked = bool(fixed and day and (member.staff_id, day, slot.label) in fixed)
            if state is CellState.WORK:
                work_minutes += slot.minutes
            elif state is CellState.BREAK:
                break_minutes += slot.minutes
            if run_state is not state:
                if run_state is not None and run_state is not CellState.OFF and run_end > run_start:
                    bars.append(GanttBar(run_state, run_start, run_end, run_locked))
                run_state = state
                run_start = slot.start_minutes
                run_end = slot.end_minutes
                run_locked = locked
            else:
                run_end = max(run_end, slot.end_minutes)
                run_locked = run_locked or locked
        if run_state is not None and run_state is not CellState.OFF and run_end > run_start:
            bars.append(GanttBar(run_state, run_start, run_end, run_locked))
        rows.append(
            GanttRow(
                staff_id=member.staff_id,
                name=member.name,
                bars=tuple(bars),
                work_minutes=work_minutes,
                break_minutes=break_minutes,
            )
        )
    return rows


def _pct(value: int, axis_start: int, span: int) -> float:
    """時間軸上の位置をパーセントに換算する。"""
    if span <= 0:
        return 0.0
    return round((value - axis_start) * 100.0 / span, 3)


def _grid_gradient(axis_start: int, axis_end: int) -> str:
    """1 時間ごとの縦罫線を引く ``background-image`` を返す。"""
    span = axis_end - axis_start
    if span <= 0:
        return ""
    first = axis_start + 60 - (axis_start % 60)
    if first >= axis_end:
        return ""
    pct = _pct(first, axis_start, span)
    return (
        "repeating-linear-gradient(90deg, transparent 0, "
        f"transparent calc({pct}% - 1px), rgba(49, 51, 63, 0.12) calc({pct}% - 1px), "
        f"rgba(49, 51, 63, 0.12) {pct}%)"
    )


def _bar_html(row: GanttRow, bar: GanttBar, axis_start: int, span: int) -> str:
    """ブロック 1 本の ``<span>`` を組み立てる。"""
    left = _pct(bar.start_minutes, axis_start, span)
    width = max(_pct(bar.end_minutes, axis_start, span) - left, 0.4)
    work = bar.state is CellState.WORK
    kind = "work" if work else "break"
    tip = (
        f"{row.label} {CellState.WORK.value if work else CellState.BREAK.value} "
        f"{clock_text(bar.start_minutes)}〜{clock_text(bar.end_minutes)}（{bar.hours:.1f}h）"
    )
    if bar.locked:
        tip = f"{LOCK} {tip}"
    inner = f"{bar.hours:.1f}h" if width >= BAR_TEXT_MIN_PCT else ""
    classes = f"shiftai-gantt-bar shiftai-gantt-bar--{kind}"
    if bar.locked:
        classes += " shiftai-gantt-bar--locked"
    return (
        f'<span class="{classes}" style="left:{left}%;width:{width}%" title="{escape(tip)}">'
        f"{escape(inner)}</span>"
    )


def _row_html(row: GanttRow, axis_start: int, span: int, grid: str) -> str:
    """職員 1 行（氏名＋時間軸＋時間合計）の ``<div>`` を組み立てる。"""
    bars = "".join(_bar_html(row, bar, axis_start, span) for bar in row.bars)
    if not bars:
        bars = '<span class="shiftai-gantt-empty">勤務なし</span>'
    background = f' style="background-image:{grid}"' if grid else ""
    return (
        '<div class="shiftai-gantt-row">'
        f'<div class="shiftai-gantt-name"><span class="shiftai-gantt-id">'
        f"{escape(row.staff_id)}</span>{escape(row.name)}</div>"
        f'<div class="shiftai-gantt-track"{background}>{bars}</div>'
        f'<div class="shiftai-gantt-hours">{row.work_minutes / 60:.1f}h</div>'
        "</div>"
    )


def gantt_html(
    shift_day: ShiftDay | None,
    slots: Sequence[Slot],
    staff: Sequence[Any],
    *,
    day: date | None = None,
    fixed: dict[tuple[str, date, str], Any] | None = None,
) -> str:
    """日別ガントチャートの HTML を組み立てる（描画はしない）。

    ``shift_grid_frame`` と同じ範囲（1 日分・同じ職員順）を受け取るので、
    表示形式だけを切り替えても同じ人員・同じ時間帯が並ぶ。
    """
    axis_start, axis_end = axis_range(slots)
    span = max(axis_end - axis_start, 0)
    rows = gantt_rows(shift_day, slots, staff, day=day, fixed=fixed)
    ticks = axis_ticks(axis_start, axis_end)
    last = len(ticks) - 1
    head_ticks = []
    for index, (minutes, label) in enumerate(ticks):
        offset = "0" if index == 0 else ("-100%" if index == last else "-50%")
        head_ticks.append(
            f'<span class="shiftai-gantt-tick" style="left:{_pct(minutes, axis_start, span)}%;'
            f'transform:translateX({offset})">{escape(label)}</span>'
        )
    grid = _grid_gradient(axis_start, axis_end)
    body = "".join(_row_html(row, axis_start, span, grid) for row in rows)
    return (
        '<div class="shiftai-gantt"><div class="shiftai-gantt-scroll">'
        '<div class="shiftai-gantt-head">'
        '<div class="shiftai-gantt-name-head">職員</div>'
        f'<div class="shiftai-gantt-axis">{"".join(head_ticks)}</div>'
        '<div class="shiftai-gantt-hours-head">時間</div>'
        "</div>"
        f"{body}</div></div>"
    )


def render(
    shift_day: ShiftDay | None,
    slots: Sequence[Slot],
    staff: Sequence[Any],
    *,
    day: date | None = None,
    fixed: dict[tuple[str, date, str], Any] | None = None,
) -> None:
    """日別ガントチャートを描画する（タブ4 の一覧置き換え表示）。"""
    if shift_day is None or not slots:
        st.info("この日のシフトがありません。")
        return
    st.markdown(gantt_html(shift_day, slots, staff, day=day, fixed=fixed), unsafe_allow_html=True)
    st.caption(
        "横棒の長さは時間帯の長さと同じです。緑＝勤務、斜線＝休憩、"
        f"右端の「時間」は勤務時間の合計（休憩は含みません）。{LOCK} は手動確定のセルです。"
    )
