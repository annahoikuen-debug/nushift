"""シフトグリッド描画・凡例・KPIカード・違反一覧などの共通ウィジェット。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
import streamlit as st

from shiftai import gap_analysis, solver, standards
from shiftai.config import (
    COLOR_BREAK,
    COLOR_OFF,
    COLOR_SHORTFALL,
    COLOR_WORK,
)
from shiftai.domain import (
    CellState,
    RequirementTable,
    ShiftDay,
    Slot,
    SolveResult,
    SolveStatus,
    Violation,
    ViolationSeverity,
    japanese_weekday,
)
from shiftai.ui import theme

LOCK = "🔒"
CELL_STATES: tuple[str, ...] = tuple(state.value for state in CellState)

FALLBACK_VIOLATION_LABELS: dict[str, str] = {
    "COVERAGE_SHORTFALL": "配置基準を満たしていません",
    "QUALIFIED_SHORTFALL": "保育士数が基準を下回っています",
    "TWO_PER_ROOM_VIOLATION": "2名ルールに抵触しています",
    "DAILY_HOURS_EXCEEDED": "1日の勤務時間が上限超過",
    "WEEKLY_HOURS_EXCEEDED": "週の勤務時間が上限超過",
    "MONTHLY_HOURS_EXCEEDED": "月間の勤務時間が上限超過",
    "MAX_WEEKLY_DAYS_EXCEEDED": "週最大出勤日数を超過",
    "MAX_CONSECUTIVE_DAYS_EXCEEDED": "最大連続勤務日数を超過",
    "BREAK_MISSING": "休憩が取得できていません",
    "BREAK_OVERLAP": "休憩が重なっています",
    "REST_HOURS_VIOLATION": "勤務間の休息時間が不足",
    "UNAVAILABLE_ASSIGNED": "希望休・不在日に勤務しています",
    "PREFERENCE_MISSED": "個人の希望を叶えられていません",
    "SHIFT_LENGTH_EXCEEDED": "1勤務の連続時間が長すぎます",
    "SOLVER_TIME_LIMIT": "ソルバが時間制限に達しました",
    "SOLVER_INFEASIBLE": "解なし（制約が矛盾）",
}

HEAT_SCALES: dict[str, tuple[tuple[int, int, int], ...]] = {
    "warm": ((255, 247, 236), (253, 208, 162), (244, 165, 130), (214, 96, 77), (153, 0, 0)),
    "shortfall": ((165, 0, 38), (215, 48, 39), (255, 191, 191), (199, 233, 192), (0, 104, 55)),
}
"""ヒートマップの 低→高 の色見本。"""

STATUS_TONE: dict[SolveStatus, str] = {
    SolveStatus.OPTIMAL: "success",
    SolveStatus.FEASIBLE: "success",
    SolveStatus.PARTIAL: "warning",
    SolveStatus.INFEASIBLE: "error",
    SolveStatus.ERROR: "error",
}

STATUS_HINT: dict[SolveStatus, str] = {
    SolveStatus.OPTIMAL: "すべての重み付き目的関数を最小化する最適解が得られました。",
    SolveStatus.FEASIBLE: "時間制限内で実行可能解が得られました。最適とは限りません。",
    SolveStatus.PARTIAL: "解が完全には出ませんでした。貪欲法による暫定シフトを含む可能性があります。",
    SolveStatus.INFEASIBLE: "制約が矛盾しており解がありません。職員数や基準を見直す必要があります。",
    SolveStatus.ERROR: "ソルバ実行中にエラーが発生しました。",
}


def violation_labels() -> dict[str, str]:
    """``gap_analysis.VIOLATION_CODE_LABELS`` があればそれを、無ければ内置の辞書を使う。"""
    found = getattr(gap_analysis, "VIOLATION_CODE_LABELS", None)
    if isinstance(found, dict) and found:
        return {**FALLBACK_VIOLATION_LABELS, **{str(k): str(v) for k, v in found.items()}}
    return dict(FALLBACK_VIOLATION_LABELS)


def violation_title(code: str) -> str:
    """違反コードを見出し用の日本語名に変換する。"""
    return violation_labels().get(str(code), str(code))


def metric_row(
    items: Sequence[tuple[str, Any, str | None]],
    *,
    per_row: int = 5,
) -> None:
    """KPI カードを行状に並べる。

    ``items`` の各要素は ``(label, value, note)``。``note`` は ``None`` で省略可。
    値は先頭の ``!`` で警告色（赤）、``~`` で情報色（青）になる。
    """
    if not items:
        return
    per_row = max(1, int(per_row))
    for start in range(0, len(items), per_row):
        group = items[start : start + per_row]
        columns = st.columns(per_row)
        for column, (label, value, note) in zip(columns, group, strict=False):
            tone = ""
            text = str(value)
            if text.startswith("!"):
                tone = " warn"
                text = text[1:]
            elif text.startswith("~"):
                tone = " info"
                text = text[1:]
            with column:
                body = (
                    f'<div class="shiftai-metric{tone}">'
                    f'<div class="shiftai-metric-label">{label}</div>'
                    f'<div class="shiftai-metric-value">{text}</div>'
                )
                if note:
                    body += f'<div class="shiftai-metric-note">{note}</div>'
                body += "</div>"
                st.markdown(body, unsafe_allow_html=True)


def cell_legend() -> None:
    """シフトセルの凡例。"""
    st.markdown(
        '<div class="shiftai-legend">'
        f'<span class="shiftai-chip" style="background:{COLOR_WORK};color:#ffffff">'
        f"<b>{CellState.WORK.value}</b>（保育・延長保育）</span>"
        f'<span class="shiftai-chip" style="background:{COLOR_BREAK}">'
        f"<b>{CellState.BREAK.value}</b>（労働基準法第9条の休憩）</span>"
        f'<span class="shiftai-chip" style="background:{COLOR_OFF};color:#424242">'
        f"<b>{CellState.OFF.value}</b>（非勤務）</span>"
        f'<span class="shiftai-chip" style="background:#ffffff;color:#6a1b9a">'
        f"{LOCK} 手動で確定したセル（再最適化しても動きません）</span>"
        "</div>",
        unsafe_allow_html=True,
    )


def staff_labels(staff: Sequence[Any]) -> list[tuple[str, str]]:
    """職員ID → ``"S01 氏名"`` のラベル一覧。"""
    return [(m.staff_id, f"{m.staff_id} {m.name}") for m in staff]


def row_label(staff_id: str, name: str) -> str:
    """グリッドの index に出すラベル。"""
    return f"{staff_id} {name}"


def shift_grid_frame(
    shift_day: ShiftDay | None,
    slots: Sequence[Slot],
    staff: Sequence[Any],
    *,
    day: date | None = None,
    fixed: dict[tuple[str, date, str], Any] | None = None,
) -> pd.DataFrame:
    """職員×時間帯のシフト表を DataFrame にする（index は ``"職員ID 氏名"``）。"""
    columns = [s.label for s in slots]
    if shift_day is None:
        return pd.DataFrame(columns=columns, index=pd.Index([], name="職員"))
    records: list[dict[str, Any]] = []
    for member in staff:
        row: dict[str, Any] = {}
        for slot in slots:
            value = shift_day.get(member.staff_id, slot).value
            if day is not None and fixed and (member.staff_id, day, slot.label) in fixed:
                value = f"{LOCK} {value}"
            row[slot.label] = value
        records.append(row)
    index = pd.Index(
        [row_label(m.staff_id, m.name) for m in staff], name="職員（ID 氏名）"
    )
    return pd.DataFrame.from_records(records, columns=columns, index=index)


def _lerp(stops: Sequence[tuple[int, int, int]], t: float) -> tuple[int, int, int]:
    """色見本の上の 1 点を線形補間する。"""
    if t <= 0.0:
        return stops[0]
    if t >= 1.0:
        return stops[-1]
    position = t * (len(stops) - 1)
    index = int(position)
    frac = position - index
    low, high = stops[index], stops[index + 1]
    return tuple(int(round(low[k] + (high[k] - low[k]) * frac)) for k in range(3))


def heat_styler(
    frame: pd.DataFrame,
    *,
    scale: str = "warm",
    vmin: float | None = None,
    vmax: float | None = None,
    subset: Sequence[str] | None = None,
    fmt: str = "{:.0f}",
) -> Any:
    """依存ライブラリなしでheatmapの色付けをする ``Styler`` を返す。

    ``Styler.background_gradient`` は matplotlib を要求するが、この環境に
    matplotlib は無いため、同等の補間を自前で実装している。
    """
    if frame.empty:
        return frame
    stops = HEAT_SCALES.get(scale, HEAT_SCALES["warm"])
    numeric = frame.apply(pd.to_numeric, errors="coerce")
    if vmin is None or vmax is None:
        values = numeric.to_numpy(dtype="float64", na_value=float("nan"))
        finite = values[np.isfinite(values)]
    if vmin is None:
        low = float(finite.min()) if finite.size else 0.0
    else:
        low = float(vmin)
    if vmax is None:
        high = float(finite.max()) if finite.size else 0.0
    else:
        high = float(vmax)
    if not (np.isfinite(low) and np.isfinite(high)) or high <= low:
        low, high, span = 0.0, 1.0, 1.0
    else:
        span = high - low

    def paint(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        if not np.isfinite(number):
            return ""
        ratio = (number - low) / span
        red, green, blue = _lerp(stops, ratio)
        ink = "#ffffff" if ratio > 0.62 else "#1f2430"
        return f"background-color: rgb({red},{green},{blue}); color: {ink};"

    columns = list(subset) if subset else None
    style = frame.style.map(paint, subset=columns) if columns else frame.style.map(paint)
    format_cols = columns if columns else [c for c in frame.columns if frame[c].dtype.kind in "ifb"]
    if format_cols:
        style = style.format({col: fmt for col in format_cols}, na_rep="—")
    else:
        style = style.format(na_rep="—")
    return style


def _cell_style(value: Any) -> str:
    text = str(value)
    locked = text.startswith(LOCK)
    if locked:
        text = text[len(LOCK) :].strip()
    if text == CellState.WORK.value:
        return f"background-color: {COLOR_WORK}33; color: {COLOR_WORK};"
    if text == CellState.BREAK.value:
        return f"background-color: {COLOR_BREAK}55; color: #7a5200;"
    if locked:
        return f"background-color: {COLOR_OFF}; box-shadow: inset 0 0 0 2px #6a1b9a;"
    return f"background-color: {COLOR_OFF}88; color: #616161;"


def style_shift_grid(frame: pd.DataFrame) -> Any:
    """シフト表に勤務/休憩/オフの色を付ける（pandas 3.0 では ``Styler.map`` を使う）。"""
    if frame.empty:
        return frame
    return frame.style.map(_cell_style).set_table_styles(
        [
            {
                "selector": "th",
                "props": [
                    ("font-size", "0.72rem"),
                    ("text-align", "center"),
                    ("white-space", "nowrap"),
                ],
            },
            {
                "selector": "td",
                "props": [("font-size", "0.78rem"), ("text-align", "center")],
            },
            {
                "selector": "th.row_heading",
                "props": [("text-align", "left"), ("white-space", "nowrap")],
            },
        ]
    )


def editable_grid_frame(
    shift_day: ShiftDay | None, slots: Sequence[Slot], staff: Sequence[Any]
) -> pd.DataFrame:
    """``st.data_editor`` に渡す素のセル状態表。

    index は 0 始まり（差分検出のため）。職員の対応付けは先頭の列 ``職員`` で行う。
    """
    columns = ["職員", *[s.label for s in slots]]
    if shift_day is None:
        return pd.DataFrame(columns=columns)
    records: list[dict[str, Any]] = []
    for member in staff:
        row: dict[str, Any] = {
            "職員": row_label(member.staff_id, member.name)
        }
        for slot in slots:
            row[slot.label] = shift_day.get(member.staff_id, slot).value
        records.append(row)
    return pd.DataFrame.from_records(records, columns=columns)


def editable_column_config(slots: Sequence[Slot]) -> dict[str, Any]:
    """セル状態表の ``column_config``。"""
    config: dict[str, Any] = {
        "職員": st.column_config.TextColumn("職員（ID 氏名）", width="medium")
    }
    for slot in slots:
        config[slot.label] = st.column_config.SelectboxColumn(
            slot.label,
            options=list(CELL_STATES),
            required=True,
            help=f"{slot.start.strftime('%H:%M')}-{slot.end.strftime('%H:%M')}",
        )
    return config


def diff_edits(
    edited: pd.DataFrame | None, baseline: pd.DataFrame | None
) -> dict[tuple[int, str], str]:
    """編集結果と基準表の差分を ``{(行index, 時間帯): 新しい値}`` で返す。"""
    if edited is None or baseline is None:
        return {}
    if edited.empty or baseline.empty:
        return {}
    changes: dict[tuple[int, str], str] = {}
    height = min(len(edited), len(baseline))
    for column in baseline.columns:
        if column == "職員" or column not in edited.columns:
            continue
        left = baseline[column].astype(str).to_numpy()[:height]
        right = edited[column].astype(str).to_numpy()[:height]
        for row in range(height):
            if left[row] != right[row]:
                changes[(row, str(column))] = right[row]
    return changes


def staff_day_summary_frame(
    shift_day: ShiftDay | None, slots: Sequence[Slot], staff: Sequence[Any]
) -> pd.DataFrame:
    """職員ごとの当日勤務サマリ（開始/終了/勤務/休憩）。"""
    records = []
    for member in staff:
        work_minutes = 0
        break_minutes = 0
        first: Slot | None = None
        last: Slot | None = None
        states = []
        if shift_day is not None:
            for slot in slots:
                state = shift_day.get(member.staff_id, slot)
                states.append(state)
                if state is CellState.WORK:
                    work_minutes += slot.minutes
                    first = first or slot
                    last = slot
                elif state is CellState.BREAK:
                    break_minutes += slot.minutes
        records.append(
            {
                "職員ID": member.staff_id,
                "氏名": member.name,
                "資格": member.primary_role.value,
                "勤務開始": first.start.strftime("%H:%M") if first else "—",
                "勤務終了": last.end.strftime("%H:%M") if last else "—",
                "勤務分数": work_minutes,
                "休憩分数": break_minutes,
                "実働時間": round(work_minutes / 60.0, 2),
            }
        )
    return pd.DataFrame.from_records(records)


def weekday_pivot_frame(
    result: SolveResult | None, slots: Sequence[Slot]
) -> pd.DataFrame:
    """職員×曜日の勤務分数ピボット表。"""
    if result is None or not result.assignments:
        return pd.DataFrame()
    records = []
    for assignment in result.assignments:
        records.append(
            {
                "職員ID": assignment.staff_id,
                "曜日": japanese_weekday(assignment.day),
                "分数": assignment.slot.minutes
                if assignment.state in (CellState.WORK, CellState.BREAK)
                else 0,
            }
        )
    frame = pd.DataFrame.from_records(records)
    if frame.empty:
        return frame
    pivot = frame.pivot_table(
        index="職員ID",
        columns="曜日",
        values="分数",
        aggfunc="sum",
        fill_value=0,
    )
    order = [w for w in "月火水木金土日" if w in pivot.columns]
    pivot = pivot[order]
    pivot.columns = [f"{c}(h)" for c in pivot.columns]
    pivot = pivot / 60.0
    pivot["合計(h)"] = pivot.sum(axis=1).round(2)
    return pivot.round(2)


def staff_hours_frame(
    result: SolveResult | None, staff: Sequence[Any]
) -> pd.DataFrame:
    """職員別勤務時間（bar_chart 用）。"""
    if result is None or not staff:
        return pd.DataFrame()
    hours = solver.staff_work_hours(result, staff)
    counts = solver.staff_shift_count(result)
    return pd.DataFrame.from_records(
        [
            {
                "職員ID": m.staff_id,
                "氏名": m.name,
                "勤務時間": float(hours.get(m.staff_id, 0.0)),
                "出勤日数": int(counts.get(m.staff_id, 0)),
            }
            for m in staff
        ]
    )


def staffing_curve_frame(
    requirements: RequirementTable | None,
    result: SolveResult | None,
    day: date,
    staff: Sequence[Any],
) -> pd.DataFrame:
    """ある日の「必要人員 vs 配置人員」の重ね書き用系列。"""
    if requirements is None or result is None:
        return pd.DataFrame()
    slots = requirements.slots
    lookup = {
        (a.staff_id, a.day, a.slot.label): a.state for a in result.assignments
    }
    records = []
    for slot in slots:
        needed = requirements.needed_staff(day, slot)
        needed_q = requirements.needed_qualified(day, slot)
        actual = sum(
            1
            for m in staff
            if lookup.get((m.staff_id, day, slot.label)) is CellState.WORK
        )
        records.append(
            {
                "時間帯": slot.label,
                "必要人員": needed,
                "必要保育士数": needed_q,
                "配置人員": actual,
                "過不足": needed - actual,
            }
        )
    return pd.DataFrame.from_records(records)


def requirement_heatmap(
    requirements: RequirementTable | None, day: date | None = None
) -> pd.DataFrame:
    """時間帯×年齢クラスの必要人員ヒートマップ用 DataFrame。"""
    if requirements is None:
        return pd.DataFrame()
    target = day or (requirements.all_days()[0] if requirements.all_days() else None)
    if target is None:
        return pd.DataFrame()
    rows = requirements.for_day(target)
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame.from_records(
        [
            {"時間帯": r.slot.label, "年齢クラス": r.age_class.value, "必要人員": r.needed_staff}
            for r in rows
        ]
    )
    if frame.empty:
        return frame
    pivot = frame.pivot_table(
        index="年齢クラス", columns="時間帯", values="必要人員", aggfunc="sum", fill_value=0
    )
    columns = [s.label for s in requirements.slots if s.label in pivot.columns]
    pivot = pivot.reindex(columns=columns).fillna(0).astype(int)
    age_order = [
        a.value
        for a in sorted(
            {r.age_class for r in rows}, key=lambda x: x.sort_key
        )
    ]
    pivot = pivot.reindex(index=[a for a in age_order if a in pivot.index])
    pivot["合計"] = pivot.sum(axis=1)
    pivot.loc["合計"] = pivot.sum(axis=0)
    return pivot


def status_banner(result: SolveResult | None) -> None:
    """``SolveResult.status`` を日本語化したバナーを出す。"""
    if result is None:
        return
    tone = STATUS_TONE.get(result.status, "info")
    label = getattr(result.status, "value", str(result.status))
    stats = result.stats or {}
    detail = STATUS_HINT.get(result.status, "")
    if stats.get("elapsed_sec") is not None:
        detail = (
            f"{detail}（所要 {float(stats['elapsed_sec']):.1f} 秒 / "
            f"ソルバ {stats.get('solver', '-')} / 変数 {stats.get('num_variables', '-')} / "
            f"制約 {stats.get('num_constraints', '-')}）"
        )
    if tone == "error":
        st.error(f"**{label}** — {detail}")
    elif tone == "warning":
        st.warning(f"**{label}** — {detail}")
    else:
        st.success(f"**{label}** — {detail}")


def gap_table_frame(report: Any) -> pd.DataFrame:
    """``GapReport`` を DataFrame にする。"""
    if report is None:
        return pd.DataFrame()
    return report.to_dataframe()


def style_gap_table(frame: pd.DataFrame) -> Any:
    """不足セルを赤、過剰セルを青にする。"""
    if frame.empty:
        return frame

    def _paint(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        if number > 0:
            return f"background-color: {COLOR_SHORTFALL}30; color: #8e0000; font-weight:600;"
        if number < 0:
            return "background-color: #1E88E51a; color: #0d47a1;"
        return ""

    style = frame.style.map(_paint, subset=["不足"])
    if "過剰" in frame.columns:
        style = style.map(
            lambda v: "background-color: #1E88E530; color: #0d47a1;"
            if float(v or 0) > 0
            else "",
            subset=["過剰"],
        )
    return style.set_table_styles(
        [
            {"selector": "th", "props": [("font-size", "0.75rem")]},
            {"selector": "td", "props": [("font-size", "0.8rem")]},
        ]
    )


def violation_dataframe(violations: Sequence[Violation]) -> pd.DataFrame:
    """``Violation`` 一覧を DataFrame にする。"""
    if not violations:
        return pd.DataFrame(columns=["深刻度", "区分", "内容", "日付", "時間帯", "職員ID"])
    labels = violation_labels()
    return pd.DataFrame.from_records(
        [
            {
                "深刻度": v.severity.value,
                "区分": labels.get(v.code, v.code),
                "コード": v.code,
                "内容": v.message,
                "日付": v.day.isoformat() if v.day else "—",
                "曜日": japanese_weekday(v.day) if v.day else "",
                "時間帯": v.slot.label if v.slot else "—",
                "職員ID": v.staff_id or "—",
            }
            for v in violations
        ]
    )


def render_violations(violations: Sequence[Violation]) -> None:
    """深刻度ごとに ``st.expander`` で分けて違反表を表示する。"""
    if not violations:
        st.success("法令違反・要調整項目は検出されていません。")
        return
    labels = violation_labels()
    for severity in (
        ViolationSeverity.BLOCKER,
        ViolationSeverity.WARNING,
        ViolationSeverity.INFO,
    ):
        group = [v for v in violations if v.severity is severity]
        if not group:
            continue
        tone = {
            ViolationSeverity.BLOCKER: "🔴",
            ViolationSeverity.WARNING: "🟠",
            ViolationSeverity.INFO: "🔵",
        }[severity]
        with st.expander(f"{tone} {severity.value}（{len(group)} 件）", expanded=severity is ViolationSeverity.BLOCKER):
            by_code: dict[str, list[Violation]] = {}
            for violation in group:
                by_code.setdefault(violation.code, []).append(violation)
            ordered = sorted(by_code.items(), key=lambda kv: -len(kv[1]))
            for code, items in ordered:
                st.markdown(
                    f"**{labels.get(code, code)}**（`{code}`） — {len(items)} 件"
                )
                st.dataframe(
                    violation_dataframe(items),
                    width="stretch",
                    hide_index=True,
                    key=f"violations_{severity.value}_{code}",
                )


def load_issue_dataframe(issues: Sequence[Any]) -> pd.DataFrame:
    """``LoadIssue`` 一覧を DataFrame にする。"""
    if not issues:
        return pd.DataFrame(columns=["深刻度", "行", "列", "内容"])
    return pd.DataFrame.from_records(
        [
            {
                "深刻度": getattr(issue, "level", ""),
                "行": int(getattr(issue, "row", -1)) + 1,
                "列": getattr(issue, "column", "") or "—",
                "内容": getattr(issue, "message", ""),
            }
            for issue in issues
        ]
    )


def requirement_basis_rows(
    requirements: RequirementTable | None, day: date | None
) -> list[tuple[str, str]]:
    """1日分の必要人員の根拠テキストを ``(時間帯×年齢クラス, 説明)`` で返す。"""
    if requirements is None or day is None:
        return []
    from shiftai.ui import state

    standard = state.current_standard()
    rows = requirements.for_day(day)
    return [
        (f"{r.slot.label}／{r.age_class.value}", standards.explain_requirement(r, standard))
        for r in sorted(rows, key=lambda x: (x.slot.start, x.age_class.sort_key))
    ]


def days_of(result: SolveResult | None) -> list[date]:
    """``SolveResult`` が持つ日付を返す。"""
    if result is None:
        return []
    return [sd.day for sd in result.shift_days]


def day_options(result: SolveResult | None) -> list[date]:
    """日付 selectbox 用の選択肢（降順・最新日先頭）。"""
    options = days_of(result)
    return sorted(options, reverse=True)


def format_day(day: date) -> str:
    """``format_jp_date`` と同じ表記。selectbox の ``format_func`` 向け。"""
    return theme.format_day(day)
