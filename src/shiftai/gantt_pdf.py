"""日別ガントチャートを A4 縦 1 枚の PDF にする。

画面側の :mod:`shiftai.ui.gantt` は同じデータを CSS パーセントの ``<span>`` で
描いている。PDF では同じレイアウト（職員が 1 行・時間帯が 1 本の時間軸）を
``reportlab`` のプリミティブで描き直す。``gantt_rows`` を共有するので、
画面と PDF で人数・時間帯・ブロックの切り方が食い違わない。

和文は OS にインストールされた日本語 TrueType ゴシック体（``msgothic.ttc`` など）を
埋め込んで描く。reportlab 同梱の CID フォント（``STSong-Light``）は **フォールバック
としてのみ**使う。理由は 2 つ:

* CID フォントは CMap で Unicode→CID を引くが、wheel に CMap データが入っていないと
  引けず、未対応文字（``〜`` U+301C など）が **黙って空白になる**。
* TrueType は cmap で glyph を引くので、字形が無くても豆腐（.notdef）として
  見える。**出ている字と出ていない字が区別できる**。

ASCII（職員ID・目盛・凡例）は和文フォントと分けず ``Helvetica`` で描く。和文フォント
は半角も全角幅で扱うため、英文を同じフォントに任せると「``S001`` が名前に重なる」
事故が起きるため。
"""

from __future__ import annotations

import io
import os
from collections.abc import Sequence
from datetime import date
from typing import Any

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdfcanvas

from shiftai import config
from shiftai.domain import CellState, ShiftDay, Slot, format_jp_date_full
from shiftai.ui.gantt import GanttBar, GanttRow, axis_range, axis_ticks, clock_text, gantt_rows

#: 和文に使う reportlab 同梱の CID フォント。TTF を同梱しないため環境非依存。
PDF_FONT = "STSong-Light"
#: 半角英数字（職員ID・目盛・凡例）用。和文フォントは半角を全角扱いLERため別体系。
ASCII_FONT = "Helvetica"
ASCII_FONT_BOLD = "Helvetica-Bold"
#: 登録名。:func:`_ensure_fonts` が和文フォントの実体をこの名前で決める。
JP_FONT = "shiftai-jp-gothic"

#: 埋め込みを試す日本語 TrueType。全てゴシック体。A4 のシフト表は読みやすさが
#: 命なので明朝にはしない。
JP_FONT_CANDIDATES: tuple[tuple[str, int], ...] = (
    (r"C:\Windows\Fonts\msgothic.ttc", 0),  # MS Gothic（Windows 標準・常にいる）
    (r"C:\Windows\Fonts\meiryo.ttc", 0),  # Meiryo
    (r"C:\Windows\Fonts\YuGothM.ttc", 0),  # Yu Gothic
    (r"C:\Windows\Fonts\BIZ-UDGothicR.ttc", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf", 0),  # Debian / Ubuntu
    ("/usr/share/fonts/opentype/noto/NotoSansJP-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/fonts-jp-gothic.ttf", 0),
    ("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", 0),  # macOS
    ("/System/Library/Fonts/Supplemental/Osaka.ttf", 0),
)

#: A4 縦。余白 12mm。
PAGE_SIZE = A4
MARGIN = 12 * mm
#: 列幅。職員名 / 時間軸 / 勤務合計。列間は TRACK_GAP だけ空ける。
NAME_COL = 38 * mm
HOURS_COL = 15 * mm
TRACK_GAP = 3 * mm
TRACK_WIDTH = PAGE_SIZE[0] - 2 * MARGIN - NAME_COL - HOURS_COL - 3 * TRACK_GAP
TRACK_X = MARGIN + NAME_COL + 2 * TRACK_GAP

#: 行高。人数が少なければ上限まで広げて紙面を使い切り、多ければ下限まで縮めて
#: ページ送りする。下限は「氏名と職員ID を 2 段に収められる」最低限。
ROW_HEIGHT_MAX = 9.5 * mm
ROW_HEIGHT_MIN = 5.0 * mm
#: この行高未満では「職員ID を名前に重ねる 2 段組」を諦め、1 行にインラインで書く。
COMPACT_ROW_HEIGHT = 6.4 * mm
#: この行高未満では棒の中に時間を書かない（字が棒からはみ出す）。
BAR_TEXT_ROW_HEIGHT = 5.6 * mm
#: トラック（時間軸）内側の左右余白。棒はこの中だけを使う。
TRACK_PAD = 1.4 * mm
#: 棒の内側に ``7.5h`` を出す最小の棒幅。
BAR_TEXT_MIN = 13 * mm

#: 見出し付近の縦位置（紙面の上端からの引き）。
TITLE_DY = 4.6 * mm
SUBTITLE_DY = 9.4 * mm
RULE_DY = 11.8 * mm
AXIS_TOP_DY = 17.0 * mm
HEADER_DY = 23.0 * mm
BODY_TOP_DY = 24.4 * mm

#: 色。``ui.theme`` の CSS と同じ値を使う。
INK = HexColor(config.COLOR_INK)
MUTED = Color(0.42, 0.44, 0.49)
GRID = Color(0.79, 0.80, 0.83)
BAND = Color(0.972, 0.973, 0.979)
TRACK_FILL = Color(0.988, 0.988, 0.992)
TRACK_EDGE = Color(0.84, 0.85, 0.88)
RULE = Color(0.58, 0.60, 0.65)
WORK_FILL = HexColor(config.COLOR_WORK)
BREAK_FILL = HexColor(config.COLOR_BREAK)
BREAK_INK = HexColor(config.COLOR_BREAK_INK)
FIXED_INK = HexColor(config.COLOR_FIXED)

_registered = False
#: :func:`_ensure_fonts` が解決した和文フォントの登録名。描画中は必ず設定済み。
jp_font = PDF_FONT


def _ensure_fonts() -> str:
    """和文フォントを登録して、その登録名を返す（1 度だけ呼ぶ）。

    日本語 TrueType が OS にあればそれを、なければ reportlab 同梱の CID フォントを
    使う。戻り値は :data:`jp_font` にも設定する。
    """
    global _registered, jp_font
    if _registered:
        return jp_font
    for path, subfont in JP_FONT_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont(JP_FONT, path, subfontIndex=subfont))
        except Exception:  # noqa: BLE001 - 読めないフォントは次へ（描画は止めない）
            continue
        _registered, jp_font = True, JP_FONT
        return jp_font
    if PDF_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT))
    _registered, jp_font = True, PDF_FONT
    return jp_font


def _is_ascii(char: str) -> bool:
    """和文フォントに任せても幅が壊れない半角文字か。"""
    return " " <= char <= "~"


def _font_for(char: str, bold: bool = False) -> str:
    """文字 1 文字に引くフォント登録名。"""
    if _is_ascii(char):
        return ASCII_FONT_BOLD if bold else ASCII_FONT
    return jp_font


def _segment(text: str, size: float, bold: bool) -> list[tuple[str, str, float]]:
    """``(文字列, フォント名, 幅)`` の run に切る。

    幅は **run 全体をまとめて** 測り直す。1 文字ずつ測った値を足すと
    kerning や CID の結合でずれたまま合成され、run をまたぐと文字が潰れる。
    """
    runs: list[tuple[str, str]] = []
    for char in text:
        font = _font_for(char, bold)
        if runs and runs[-1][1] == font:
            runs[-1] = (runs[-1][0] + char, font)
        else:
            runs.append((char, font))
    return [(chunk, font, pdfmetrics.stringWidth(chunk, font, size)) for chunk, font in runs]


def _text_width(text: str, size: float, *, bold: bool = False) -> float:
    """和文 CID + 半角 Helvetica を切り替えたときの実際の描画幅。"""
    return sum(width for _text, _font, width in _segment(text, size, bold))


def _truncate(text: str, size: float, max_width: float, *, bold: bool = False) -> str:
    """``max_width`` に収まるよう ``…`` で切る。"""
    if _text_width(text, size, bold=bold) <= max_width:
        return text
    tail = "…"
    budget = max_width - _text_width(tail, size, bold=bold)
    kept = ""
    for char in text:
        if _text_width(kept + char, size, bold=bold) > budget:
            break
        kept += char
    return kept + tail


def _draw_mixed(
    target: pdfcanvas.Canvas,
    x: float,
    y: float,
    text: str,
    size: float,
    *,
    bold: bool = False,
    align: str = "left",
    max_width: float | None = None,
    color: Color | None = None,
) -> float:
    """和文と半角英数字混じりの 1 行を描く。``x`` は ``align`` UTO での基準位置。

    ``max_width`` を渡すと収まるよう ``…`` で切って描画し、描画した幅を返す。
    """
    if not text:
        return 0.0
    if max_width is not None:
        text = _truncate(text, size, max_width, bold=bold)
    total = _text_width(text, size, bold=bold)
    if align == "right":
        cursor = x - total
    elif align == "center":
        cursor = x - total / 2
    else:
        cursor = x
    target.setFillColor(color if color is not None else INK)
    for chunk, font, width in _segment(text, size, bold):
        target.setFont(font, size)
        target.drawString(cursor, y, chunk)
        cursor += width
    return total


#: 目盛ラベル同士の間隔。これ未満なら間引く（``07:15`` と ``08:00`` が重なる）。
TICK_MIN_GAP = 9.5 * mm


def _visible_ticks(ticks: Sequence[tuple[int, str]], span: int) -> list[tuple[int, str]]:
    """間隔が近すぎて重なってしまう目盛を間引く（開所・閉所は必ず残す）。

    ``ui.gantt`` 側は CSS の ``transform`` でラベルを左右に逃がして誤魔化している。
    PDF にはそれができないので、描かないという方を選ぶ。
    """
    if span <= 0 or not ticks:
        return []
    positions = [
        (minutes, label, (minutes - ticks[0][0]) / span * TRACK_WIDTH) for minutes, label in ticks
    ]
    kept: list[tuple[int, str, float]] = [positions[0]]
    for entry in positions[1:-1]:
        if entry[2] - kept[-1][2] >= TICK_MIN_GAP:
            kept.append(entry)
    last = positions[-1]
    if last[2] - kept[-1][2] < TICK_MIN_GAP and len(kept) > 1:
        kept.pop()
    kept.append(last)
    return [(minutes, label) for minutes, label, _x in kept]


def _draw_axis(
    target: pdfcanvas.Canvas, axis_start: int, axis_end: int, span: int, top: float
) -> None:
    """時間軸の目盛り（時刻）と縦罫線を描く。"""
    if span <= 0:
        return
    ticks = _visible_ticks(axis_ticks(axis_start, axis_end), span)
    last = len(ticks) - 1
    for index, (minutes, label) in enumerate(ticks):
        px = TRACK_X + (minutes - axis_start) / span * TRACK_WIDTH
        target.setStrokeColor(GRID)
        target.setLineWidth(0.25)
        target.line(px, top - 2.6 * mm, px, top - 4.6 * mm)
        align = "left" if index == 0 else ("right" if index == last else "center")
        # 端の目盛はページ外へ尼斯まなので内側へ寄せ、左右にはみ出させない。
        label_x = px + (1.0 * mm if index == 0 else (-1.0 * mm if index == last else 0))
        _draw_mixed(target, label_x, top - 1.4 * mm, label, 6.2, align=align, color=MUTED)


def _draw_track(
    target: pdfcanvas.Canvas,
    y: float,
    height: float,
    axis_start: int,
    axis_end: int,
    span: int,
) -> None:
    """時間軸の帯地色と 1 時間ごとの縦罫線を描く（棒より先に描く）。"""
    target.setFillColor(TRACK_FILL)
    target.setStrokeColor(TRACK_EDGE)
    target.setLineWidth(0.3)
    target.rect(TRACK_X, y, TRACK_WIDTH, height, stroke=1, fill=1)
    if span <= 0:
        return
    target.setStrokeColor(GRID)
    target.setLineWidth(0.25)
    mark = axis_start + 60 - (axis_start % 60)
    while mark < axis_end:
        px = TRACK_X + (mark - axis_start) / span * TRACK_WIDTH
        target.line(px, y + 0.4, px, y + height - 0.4)
        mark += 60


def _draw_bar(
    target: pdfcanvas.Canvas,
    bar: GanttBar,
    axis_start: int,
    span: int,
    y: float,
    height: float,
    *,
    with_text: bool,
) -> None:
    """職員 1 行の中の連続ブロック 1 本を描く。"""
    if span <= 0:
        return
    left = TRACK_X + (bar.start_minutes - axis_start) / span * TRACK_WIDTH
    width = max((bar.end_minutes - bar.start_minutes) / span * TRACK_WIDTH, 0.6 * mm)
    work = bar.state is CellState.WORK
    fill = WORK_FILL if work else BREAK_FILL
    target.setFillColor(fill)
    target.setStrokeColor(Color(1, 1, 1) if work else BREAK_INK)
    target.setLineWidth(0.2)
    target.rect(left, y, width, height, stroke=0, fill=1)
    if bar.locked:
        target.setStrokeColor(FIXED_INK)
        target.setLineWidth(0.8)
        target.rect(left, y, width, height, stroke=1, fill=0)
    if not with_text or width < BAR_TEXT_MIN:
        return
    size = 6.2 if height >= 4.0 * mm else 5.0
    _draw_mixed(
        target,
        left + width / 2,
        y + height / 2 - size * 0.35,
        f"{bar.hours:.1f}h",
        size,
        align="center",
        color=Color(1, 1, 1) if work else BREAK_INK,
    )


def _draw_row_heading(target: pdfcanvas.Canvas, row: GanttRow, y: float, height: float) -> None:
    """職員名・職員ID・勤務合計を描く。

    行が詰まって 2 段に収まらないときは ``S001 山田花子`` の 1 行に畳む。
    """
    middle = y + height / 2
    if height < COMPACT_ROW_HEIGHT:
        label = f"{row.staff_id} {row.name}".strip()
        _draw_mixed(target, MARGIN, middle - 2.2, label, 6.2, max_width=NAME_COL, color=INK)
    else:
        _draw_mixed(target, MARGIN, middle + 0.6, row.name, 7.0, max_width=NAME_COL, color=INK)
        _draw_mixed(
            target, MARGIN, middle - 4.2, row.staff_id, 5.4, max_width=NAME_COL, color=MUTED
        )
    _draw_mixed(
        target,
        MARGIN + NAME_COL,
        middle - 2.2,
        f"{row.work_minutes / 60:.1f}h",
        7.0,
        align="right",
        color=INK,
    )


def _draw_header(target: pdfcanvas.Canvas, y: float) -> None:
    """列見出し（職員 / 時間）と、その下の罫線。"""
    _draw_mixed(target, MARGIN, y, "職員", 6.4, bold=True, color=MUTED)
    _draw_mixed(
        target,
        MARGIN + NAME_COL,
        y,
        "時間",
        6.4,
        bold=True,
        align="right",
        color=MUTED,
    )
    target.setStrokeColor(RULE)
    target.setLineWidth(0.4)
    target.line(MARGIN, y - 1.6 * mm, PAGE_SIZE[0] - MARGIN, y - 1.6 * mm)


def _available_height() -> float:
    """職員行に使える縦幅（凡例と注記 reserving を含む）。"""
    top = PAGE_SIZE[1] - MARGIN - BODY_TOP_DY
    bottom = MARGIN + 13 * mm
    return top - bottom


def _layout_rows(row_count: int) -> tuple[int, int, float]:
    """``(総ページ数, 1 ページの行数, 行高)`` を決める。

    人数が 1 ページに収まる限りは、**行高を上限まで広げ**余白を作らない。
    収まらないときだけ最小行高まで縮めてページ送りする。
    """
    available = _available_height()
    per_page_min = max(int(available / ROW_HEIGHT_MIN), 1)
    if row_count <= per_page_min:
        return 1, row_count, min(max(available / row_count, ROW_HEIGHT_MIN), ROW_HEIGHT_MAX)
    pages = -(-row_count // per_page_min)
    per_page = -(-row_count // pages)
    return pages, per_page, min(max(available / per_page, ROW_HEIGHT_MIN), ROW_HEIGHT_MAX)


def _draw_legend(target: pdfcanvas.Canvas, y: float) -> None:
    """凡例。画面の説明と同じ意味にする。"""
    entries = (
        (WORK_FILL, Color(1, 1, 1), "勤務"),
        (BREAK_FILL, BREAK_INK, "休憩"),
        (None, FIXED_INK, "手動確定（紫枠）"),
    )
    cursor = MARGIN
    for fill, edge, label in entries:
        target.setStrokeColor(edge)
        target.setLineWidth(0.3)
        if fill is None:
            target.setFillColor(Color(1, 1, 1))
            target.rect(cursor, y, 8 * mm, 2.8 * mm, stroke=1, fill=1)
            target.setStrokeColor(FIXED_INK)
            target.setLineWidth(0.8)
            target.rect(cursor, y, 8 * mm, 2.8 * mm, stroke=1, fill=0)
        else:
            target.setFillColor(fill)
            target.rect(cursor, y, 8 * mm, 2.8 * mm, stroke=1, fill=1)
        cursor += 8 * mm + 1.6 * mm
        cursor += _draw_mixed(target, cursor, y - 0.3, label, 6.2, color=MUTED) + 5 * mm
    _draw_mixed(
        target,
        cursor,
        y - 0.3,
        "横棒の長さは時間帯の長さと同じです。右端の「時間」は勤務時間の合計（休憩は含みません）。",
        6.2,
        color=MUTED,
    )


def _draw_footer(target: pdfcanvas.Canvas, legend_y: float) -> None:
    """下部：凡例と注意書き。

    ``legend_y`` は凡例を置きたい高さ。人数が少ないページでは紙面最下部ではなく
    最終行のすぐ下に付け、下の余白が「切り忘れ」に見えないようにする。
    """
    _draw_legend(target, legend_y)
    _draw_mixed(
        target,
        PAGE_SIZE[0] - MARGIN,
        MARGIN - 1.4 * mm,
        "本紙はシフト作成アプリの出力を参考にしたものです。実際の勤務は園の指示に従ってください。",
        5.8,
        align="right",
        color=MUTED,
    )


def _draw_heading(
    target: pdfcanvas.Canvas,
    day: date | None,
    facility_name: str,
    axis_start: int,
    axis_end: int,
    row_count: int,
    page: int,
    pages: int,
) -> None:
    """表題・副題・罫線。"""
    top = PAGE_SIZE[1] - MARGIN
    _draw_mixed(target, MARGIN, top - TITLE_DY, "日別シフト ガントチャート", 13.0, bold=True)
    right = facility_name or "保育園"
    if day is not None:
        # ``format_jp_date_full`` は曜日まで含む（「10月5日(月)」）。曜日を足すと二重になる。
        right += "　" + format_jp_date_full(day)
    _draw_mixed(
        target, PAGE_SIZE[0] - MARGIN, top - TITLE_DY + 0.4, right, 8.6, align="right", color=INK
    )
    subtitle = (
        f"開所 {clock_text(axis_start)} 〜 閉所 {clock_text(axis_end)}　／　対象 {row_count} 名"
    )
    if pages > 1:
        subtitle += f"　／　{page + 1} / {pages} ページ"
    _draw_mixed(target, MARGIN, top - SUBTITLE_DY, subtitle, 6.8, color=MUTED)
    target.setStrokeColor(RULE)
    target.setLineWidth(0.4)
    target.line(MARGIN, top - RULE_DY, PAGE_SIZE[0] - MARGIN, top - RULE_DY)


def gantt_pdf_bytes(
    shift_day: ShiftDay | None,
    slots: Sequence[Slot],
    staff: Sequence[Any],
    *,
    day: date | None = None,
    fixed: dict[tuple[str, date, str], Any] | None = None,
    facility_name: str = "",
) -> bytes:
    """日別ガントチャートを A4 縦 1 枚の PDF バイト列で返す。

    職員が多すぎて 1 ページに収まらない場合のみ、続きページへsplit する
    （1 日 = 1 枚は「たいてい 1 枚」の意味になる）。
    """
    _ensure_fonts()
    rows = gantt_rows(shift_day, slots, staff, day=day, fixed=fixed)
    axis_start, axis_end = axis_range(slots)
    span = max(axis_end - axis_start, 0)
    pages, per_page, row_height = _layout_rows(len(rows))

    buffer = io.BytesIO()
    target = pdfcanvas.Canvas(buffer, pagesize=PAGE_SIZE)
    target.setTitle(f"日別ガントチャート {day.isoformat() if day else ''}".strip())
    target.setAuthor("shiftai")
    target.setSubject(facility_name)

    body_top = PAGE_SIZE[1] - MARGIN - BODY_TOP_DY
    for page in range(pages):
        chunk = rows[page * per_page : (page + 1) * per_page]
        _draw_heading(target, day, facility_name, axis_start, axis_end, len(rows), page, pages)
        _draw_axis(target, axis_start, axis_end, span, PAGE_SIZE[1] - MARGIN - AXIS_TOP_DY)
        _draw_header(target, PAGE_SIZE[1] - MARGIN - HEADER_DY)
        for index, row in enumerate(chunk):
            y = body_top - (index + 1) * row_height
            if index % 2 == 0:
                target.setFillColor(BAND)
                target.rect(
                    MARGIN - 1.5 * mm,
                    y,
                    PAGE_SIZE[0] - 2 * MARGIN + 3 * mm,
                    row_height,
                    stroke=0,
                    fill=1,
                )
            track_y = y + TRACK_PAD
            track_height = row_height - 2 * TRACK_PAD
            _draw_track(target, track_y, track_height, axis_start, axis_end, span)
            if span > 0:
                for bar in row.bars:
                    _draw_bar(
                        target,
                        bar,
                        axis_start,
                        span,
                        track_y,
                        track_height,
                        with_text=row_height >= BAR_TEXT_ROW_HEIGHT,
                    )
            else:
                _draw_mixed(target, TRACK_X, y + row_height / 2 - 2.0, "時間帯が未設定です", 6.2)
            _draw_row_heading(target, row, y, row_height)
        bottom = body_top - len(chunk) * row_height
        # 最終行が紙面下端より上にあれば、凡例もその直下へ寄せる（余白が
        # 「描画が途中切れ」に見えないようにするため）。
        legend_y = max(bottom - 7.0 * mm, MARGIN + 6.4 * mm)
        _draw_footer(target, legend_y)
        target.showPage()

    target.save()
    return buffer.getvalue()
