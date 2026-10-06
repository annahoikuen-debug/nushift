"""``shiftai.gantt_pdf`` のテスト。

画面側の ``ui.gantt`` と同じ ``gantt_rows`` から描くため、検証するのは
「A4 縦 1 枚になる」「日本語が埋まる」「1 日だけが出る」の 3 点に絞る。
"""

from __future__ import annotations

import os
from datetime import date, time

import pytest

from shiftai import gantt_pdf
from shiftai.domain import CellState, Contract, Role, ShiftDay, Slot, StaffMember

pytestmark = pytest.mark.timeout(300)


def _slots() -> list[Slot]:
    """開所 7:15 〜 閉所 19:30 の 30 分刻み。"""
    out: list[Slot] = []
    start = 7 * 60 + 15
    while start + 30 <= 19 * 60 + 30:
        out.append(
            Slot(
                start=time(start // 60, start % 60),
                end=time((start + 30) // 60, (start + 30) % 60),
            )
        )
        start += 30
    return out


def _staff(count: int) -> list[StaffMember]:
    return [
        StaffMember(
            staff_id=f"S{i:03d}",
            name=f"職員{i:02d}",
            roles=(Role.HOIKUSHI,),
            contract=Contract(weekly_hours=40.0, daily_hours=8.0),
        )
        for i in range(1, count + 1)
    ]


def _shift_day(staff: list[StaffMember], slots: list[Slot], day: date) -> ShiftDay:
    """先頭 6 名に勤務・休憩・オフを交互に割り当てる。"""
    assignments: dict[str, dict[str, CellState]] = {}
    for index, member in enumerate(staff[:6]):
        row: dict[str, CellState] = {}
        for slot_index, slot in enumerate(slots):
            if index >= 6:
                row[slot.label] = CellState.OFF
            elif slot_index % 7 == 3:
                row[slot.label] = CellState.BREAK
            elif slot_index % 3 == 0:
                row[slot.label] = CellState.OFF
            else:
                row[slot.label] = CellState.WORK
        assignments[member.staff_id] = row
    return ShiftDay(day=day, assignments=assignments)


def test_gantt_pdf_bytes_is_a4_single_page():
    day = date(2026, 9, 28)
    slots = _slots()
    staff = _staff(12)
    data = gantt_pdf.gantt_pdf_bytes(
        _shift_day(staff, slots, day), slots, staff, day=day, facility_name="あさひ保育園"
    )
    assert data.startswith(b"%PDF-")
    # /Count 1 = 1 ページ。/MediaBox は A4 縦（595x842pt）。
    assert b"/Count 1" in data
    assert b"/MediaBox [ 0 0 595.2756 841.8898 ]" in data


def test_gantt_pdf_bytes_renders_japanese_title():
    day = date(2026, 9, 28)
    slots = _slots()
    staff = _staff(3)
    data = gantt_pdf.gantt_pdf_bytes(
        _shift_day(staff, slots, day), slots, staff, day=day, facility_name="あさひ保育園"
    )
    # フォントは埋め込む（OS の TrueType サブセット、または reportlab の CID）。
    # TrueType をサブセットすると名前が F2+0 等に置き換わるので、
    # 埋め込みの有無（``/FontFile2``）で判定する。
    assert b"/FontFile2" in data or b"/Encoding /UniGB-UCS2-H" in data


def test_japanese_font_prefers_truetype_when_available():
    """OS に日本語 TrueType があればそれを使う（CID は ``〜`` などが黙って消える）。

    CID へフォールバック不可避免の環境（フォントが無い CI 等）でも
    描画は続けるので、戻り値はどちらでもよい。
    """
    font = gantt_pdf._ensure_fonts()
    assert font in (gantt_pdf.JP_FONT, gantt_pdf.PDF_FONT)
    if any(os.path.isfile(path) for path, _sub in gantt_pdf.JP_FONT_CANDIDATES):
        assert font == gantt_pdf.JP_FONT


def test_visible_ticks_never_overlap():
    """目盛が近すぎる場合は間引く（``07:15`` と ``08:00`` の重なり防止）。"""
    ticks = [
        (7 * 60 + 15, "07:15"),
        (8 * 60, "08:00"),
        (9 * 60, "09:00"),
        (19 * 60, "19:00"),
        (19 * 60 + 15, "19:15"),
    ]
    span = 12 * 60
    kept = gantt_pdf._visible_ticks(ticks, span)
    assert kept[0] == (7 * 60 + 15, "07:15")
    assert kept[-1] == (19 * 60 + 15, "19:15")
    positions = [(minutes - ticks[0][0]) / span * gantt_pdf.TRACK_WIDTH for minutes, _ in kept]
    gaps = [b - a for a, b in zip(positions, positions[1:], strict=False)]
    assert min(gaps) >= gantt_pdf.TICK_MIN_GAP - 0.01


def test_layout_rows_uses_one_page_for_normal_headcount():
    pages, per_page, height = gantt_pdf._layout_rows(28)
    assert (pages, per_page) == (1, 28)
    assert gantt_pdf.ROW_HEIGHT_MIN <= height <= gantt_pdf.ROW_HEIGHT_MAX


def test_layout_rows_paginates_instead_of_squeezing():
    """人数が多くても行高の下限は割らない。詰めて 1 枚にするよりページ送りする。"""
    pages, per_page, height = gantt_pdf._layout_rows(200)
    assert pages > 1
    assert height >= gantt_pdf.ROW_HEIGHT_MIN
    assert per_page * pages >= 200


def test_gantt_pdf_bytes_paginates_many_staff():
    day = date(2026, 9, 28)
    slots = _slots()
    staff = _staff(80)
    data = gantt_pdf.gantt_pdf_bytes(_shift_day(staff, slots, day), slots, staff, day=day)
    assert data.startswith(b"%PDF-")
    assert b"/Count 1 " not in data.split(b"/Type /Pages")[0]


def test_gantt_pdf_bytes_without_slots():
    day = date(2026, 9, 28)
    staff = _staff(2)
    data = gantt_pdf.gantt_pdf_bytes(None, [], staff, day=day)
    assert data.startswith(b"%PDF-")


def test_gantt_pdf_marks_fixed_cells():
    day = date(2026, 9, 28)
    slots = _slots()
    staff = _staff(2)
    fixed = {(staff[0].staff_id, day, slots[1].label): CellState.WORK}
    plain = gantt_pdf.gantt_pdf_bytes(_shift_day(staff, slots, day), slots, staff, day=day)
    marked = gantt_pdf.gantt_pdf_bytes(
        _shift_day(staff, slots, day), slots, staff, day=day, fixed=fixed
    )
    assert plain.startswith(b"%PDF-")
    assert marked.startswith(b"%PDF-")
    assert len(marked) != len(plain)


def test_gantt_pdf_uses_reported_slot_range():
    # 時間帯は 7:15 始まりの 30 分刻み（分は 15 刻み）なので、9:00 も 19:00 も無い。
    slots = [s for s in _slots() if s.start_minutes >= 9 * 60]
    assert gantt_pdf.axis_range(slots) == (9 * 60 + 15, 19 * 60 + 15)
    assert gantt_pdf.clock_text(gantt_pdf.axis_range(slots)[0]) == "09:15"
    assert gantt_pdf.clock_text(gantt_pdf.axis_range(slots)[1]) == "19:15"
    assert gantt_pdf.PDF_FONT == "STSong-Light"
    # 実際に 1 枚描けることの確認（ CID フォント登録の副作用も検証する）。
    day = date(2026, 9, 28)
    staff = _staff(1)
    assert (
        gantt_pdf.gantt_pdf_bytes(_shift_day(staff, slots, day), slots, staff, day=day)
    ).startswith(b"%PDF-")
