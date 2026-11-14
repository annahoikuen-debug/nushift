"""シフトグリッド描画・凡例・KPIカード・違反一覧などの共通ウィジェット。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from html import escape
from typing import Any

import numpy as np
import pandas as pd
import streamlit as st

from shiftai import fairness, gap_analysis, live_validation, solver, standards
from shiftai.config import (
    COLOR_BREAK,
    COLOR_BREAK_INK,
    COLOR_FIXED,
    COLOR_INK,
    COLOR_OFF,
    COLOR_OVER,
    COLOR_OVER_INK,
    COLOR_SHORTFALL,
    COLOR_SHORTFALL_INK,
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
    "DAILY_HOURS_EXCEEDED": "1日の勤務時間が契約上限を超えています",
    "DAILY_HOURS_SHORT": "1日の勤務時間が最低時間未満です",
    "SOLVER_TIME_LIMIT": "ソルバが時間制限に達しました",
    "SOLVER_INFEASIBLE": "解なし（制約が矛盾）",
}


FALLBACK_VIOLATION_FIX_HINTS: dict[str, str] = {
    "SHORTFALL_STAFF": "不足している時間帯に入れる職員が足りません。"
    "{shift} でその時間帯のオフの職員を「勤務」に変えるか、"
    "{data} の必要人員（職員数・保育士数）を見直してください。",
    "SHORTFALL_QUALIFIED": "不足している時間帯の保育士が足りません。"
    "{shift} で保育士資格の職員をその時間帯に配置するか、"
    "{data} の配置基準を見直してください。",
    "BREAK_OVERLAP": "同時刻の休憩が集中しています。"
    "{shift} で休憩の始まる時間帯をずらしてください。",
    "WORK_ON_UNAVAILABLE": "希望休・不在時間帯に勤務が入っています。"
    "{shift} で該当セルを「オフ」にするか、{data} の勤務不可設定を確認してください。",
    "WORK_ON_CLOSED_DAY": "休園日に勤務が入っています。"
    "{shift} で該当セルを「オフ」にするか、{data} の開園日設定を確認してください。",
    "OUTSIDE_CONTRACT_HOURS": "契約時間帯の外で勤務しています。"
    "{shift} で始業・終業を契約内に収めるか、{data} の契約時間帯を確認してください。",
    "DAILY_HOURS_EXCEEDED": "この職員の 1 日の勤務が契約上限を超えています。"
    "{shift} で勤務時間を短くするか、{data} の契約時間を確認してください。",
    "DAILY_HOURS_SHORT": "この職員の 1 日の勤務が最低勤務時間（正社員4時間以上など）を下回っています。"
    "{shift} で勤務時間を増やすか、{data} の契約設定を確認してください。",
    "OVERTIME_NOT_ALLOWED": "時間外労働の契約がないため 8 時間を超えられません。"
    "{shift} で勤務を 8 時間以内に収めるか、{data} の 36 協定設定を確認してください。",
    "OVERTIME_REQUIRES_36AGREEMENT": "時間外労働として 36 協定の締結と割増賃金の確認が必要です。"
    "{shift} で勤務を短縮するか、{data} の職員設定を確認してください。",
    "BREAK_INSUFFICIENT": "休憩が規定時間に足りません。"
    "{shift} で休憩セルを追加・延長してください。",
    "BREAK_FRAGMENTED": "休憩が分断されています。"
    "{shift} で連続した休憩になるようセルを調整してください。",
    "SPLIT_SHIFT": "勤務が分割されています。園の運用で分割勤務を許可しない場合は、"
    "{shift} で勤務ブロックを 1 つにまとめてください。",
    "CONSECUTIVE_DAYS": "連続勤務日数が上限を超えています。"
    "{shift} で連続区間の間に休みを入れてください。",
    "WEEKLY_DAYS_EXCEEDED": "週の勤務日数が上限を超えています。"
    "{shift} で該当週の休みを増やしてください。",
    "REST_HOURS_SHORT": "勤務間の休息が 11 時間未満です。"
    "{shift} で翌日の始業を遅らせるか前日の終業を早めてください。",
    "WEEKLY_HOURS_EXCEEDED": "この職員の週勤務が 44 時間を超えています。"
    "{shift} で該当週の勤務を減らしてください。",
    "MONTHLY_HOURS_EXCEEDED": "この職員の勤務時間が契約上限を超えています。"
    "{shift} で休みを増やすか、{data} の職員 CSV（契約時間）を確認してください。",
    "MONTHLY_HOURS_SHORT": "この職員の勤務時間が契約下限に届いていません。"
    "{shift} で勤務を増やすか、{data} の契約時間を確認してください。",
    "VARIABLE_HOURS_EXCEEDED": "変形労働時間制の総枠を超えています。36 協定に基づく割増賃金が必要です。"
    "{shift} で総勤務時間を減らすか、{data} の 36 協定設定を確認してください。",
    "VARIABLE_HOURS_EXCEEDED_NO_36": "変形労働時間制の総枠を超えていますが 36 協定が未締結のため違法です。"
    "{shift} で総勤務時間を減らすか、{data} で 36 協定を設定してください。",
    "AVOID_EARLY_CONFLICT": "早朝保育を希望しない職員が配置されています。"
    "{shift} で早朝のセルを「オフ」にするか、{data} の希望設定を確認してください。",
    "AVOID_LATE_CONFLICT": "延長保育を希望しない職員が配置されています。"
    "{shift} で延長のセルを「オフ」にするか、{data} の希望設定を確認してください。",
    "HOURS_IMBALANCE": "職員間の勤務時間の偏りが大きいです。1 つのセルでは直せません。"
    "{solve} で公平さの重みを上げて再計算するか、全体の勤務配分を調整してください。",
    "CHECK_UNAVAILABLE": "違反検査が完了していません。"
    "{solve} で再計算するか、時間制限を延ばして再実行してください。",
    "NOT_PLACED_ROLE": "配置対象外の職種です（調理員・栄養教諭など）。"
    "シフト・労働時間の判定対象外のため対応は不要です。",
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
    SolveStatus.OPTIMAL: "配置基準と職員の希望を最もバランスよく満たす最適シフトが完成しました。",
    SolveStatus.FEASIBLE: "配置基準を満たす実用的なシフトが完成しました。",
    SolveStatus.PARTIAL: "一部の時間帯で人員が不足しています。下の過不足表をご確認ください。",
    SolveStatus.INFEASIBLE: "職員数・希望休・配置基準の条件が衝突しています。「原因診断」をご確認ください。",
    SolveStatus.ERROR: "シフト作成中にエラーが発生しました。",
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


def metric_html(label: Any, value: Any, note: str | None) -> tuple[str, str]:
    """KPI カード 1 枚の HTML と CSS クラスを組み立てる（描画はしない）。

    **必ずエスケープする**。``label`` / ``value`` / ``note`` には
    ユーザー由来の値が入るため：

    * ``note`` にサイドバーの「園名」（``facility_name``）が flow する
      （``tab_requirements.render`` が ``(ラベル, 値, settings.facility_name)`` を渡す）
    * ``value`` に内部生成の文字列が入る

    Streamlit の ``unsafe_allow_html=True`` は**サーバー側でサニタイズしない**
    （Markdown レンダラの ``rehype-raw`` を有効にするだけ）。
    エスケープしないと、園名入力欄に
    ``<img src=x onerror=...>`` を入れただけでブラウザ上で JS が実行される。

    戻り値を分けているのは、AppTest 無しでも「この関数だけ」を
    検証できるようにするため（``tests/test_security_hardening.py``）。
    """
    tone = ""
    text = str(value)
    if text.startswith("!"):
        tone = " warn"
        text = text[1:]
    elif text.startswith("~"):
        tone = " info"
        text = text[1:]
    body = (
        f'<div class="shiftai-metric{tone}">'
        f'<div class="shiftai-metric-label">{escape(str(label))}</div>'
        f'<div class="shiftai-metric-value">{escape(text)}</div>'
    )
    if note:
        body += f'<div class="shiftai-metric-note">{escape(str(note))}</div>'
    body += "</div>"
    return body, "shiftai-metric" + tone


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
            body, _css_class = metric_html(label, value, note)
            with column:
                st.markdown(body, unsafe_allow_html=True)


def cell_legend() -> None:
    """シフトセルの凡例。"""
    st.markdown(
        '<div class="shiftai-legend">'
        f'<span class="shiftai-chip" style="background:{COLOR_WORK};color:#ffffff">'
        f"<b>{CellState.WORK.value}</b>（保育・延長保育）</span>"
        f'<span class="shiftai-chip" style="background:{COLOR_BREAK}">'
        f"<b>{CellState.BREAK.value}</b>（労働基準法第34条の休憩）</span>"
        f'<span class="shiftai-chip" style="background:{COLOR_OFF};color:#424242">'
        f"<b>{CellState.OFF.value}</b>（非勤務）</span>"
        f'<span class="shiftai-chip" style="background:#ffffff;color:{COLOR_FIXED}">'
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
    index = pd.Index([row_label(m.staff_id, m.name) for m in staff], name="職員（ID 氏名）")
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
        ink = "#ffffff" if ratio > 0.62 else COLOR_INK
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
        return f"background-color: {COLOR_BREAK}55; color: {COLOR_BREAK_INK};"
    if locked:
        return f"background-color: {COLOR_OFF}; box-shadow: inset 0 0 0 2px {COLOR_FIXED};"
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


def style_live_grid(
    frame: pd.DataFrame,
    report: live_validation.LiveReport,
    *,
    row_labels: Mapping[str, str] | None = None,
) -> Any:
    """編集中のグリッドに、違反セルの色枠を付ける（:mod:`shiftai.live_validation`）。

    ``row_labels`` は「index の値 -> 職員ID」の対応（index が「職員ID 氏名」、
    列が時間帯ラベルの表を想定）。赤＝配置基準・契約違反、橙＝運用上の注意。
    """
    if frame.empty or not report.issues:
        return style_shift_grid(frame)
    labels = dict(row_labels or {})
    error_cells = report.error_cells()
    warning_cells = report.warning_cells()
    error_cols = report.error_columns()
    warning_cols = report.warning_columns()

    def paint(row: pd.Series) -> list[str]:
        # 行ごとに CSS を列の順に伦べる
        # ``axis`` を受け取らないため、行単位の ``apply(axis=1)`` を使う）
        sid = labels.get(str(row.name), "")
        out: list[str] = []
        for column, value in row.items():
            base = _cell_style(value)
            if column in error_cols:
                out.append(base + "; border-bottom: 3px solid #C62828;")
                continue
            if column in warning_cols:
                out.append(base + "; border-bottom: 3px solid #EF6C00;")
                continue
            key = (sid, str(column))
            if key in error_cells:
                out.append(base + "; box-shadow: inset 0 0 0 2px #C62828;")
            elif key in warning_cells:
                out.append(base + "; box-shadow: inset 0 0 0 2px #EF6C00;")
            else:
                out.append(base)
        return out

    return frame.style.apply(paint, axis=1).set_table_styles(
        [
            {
                "selector": "th",
                "props": [("font-size", "0.72rem"), ("white-space", "nowrap")],
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
        row: dict[str, Any] = {"職員": row_label(member.staff_id, member.name)}
        for slot in slots:
            row[slot.label] = shift_day.get(member.staff_id, slot).value
        records.append(row)
    return pd.DataFrame.from_records(records, columns=columns)


def editable_indexed_frame(
    frame: pd.DataFrame, slots: Sequence[Slot], staff: Sequence[Any]
) -> pd.DataFrame:
    """``editable_grid_frame`` を「職員ID+氏名」を index にした色付きビュー用表にする。

    ``st.data_editor`` の返り値（index が 0 始まり、職員名が ``職員`` 列）は
    :func:`style_live_grid` で色を付けるのに都合が悪い（index が数値のままになる）ため、
    職員ラベルを index に移した複製をこの場で作る。
    """
    if frame is None or frame.empty:
        return pd.DataFrame(columns=[s.label for s in slots])
    labels = [row_label(m.staff_id, m.name) for m in staff]
    out = frame.copy()
    slot_labels = [s.label for s in slots]
    # ``st.data_editor`` は「職員」列を先頭に足すため、列数が合わない。
    # 位数だけが時間帯数なら「職員」列を落としてから列名を付け替える。
    if len(out.columns) == len(slot_labels) + 1:
        out = out.drop(columns=[out.columns[0]])
    if len(out.columns) == len(slot_labels):
        out.columns = slot_labels
    if len(out) == len(labels):
        out.index = pd.Index(labels, name="職員（ID 氏名）")
    return out


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


def fairness_frame(
    result: SolveResult | None,
    staff: Sequence[Any],
    slots: Sequence[Slot],
    *,
    standard: Any = None,
    patterns: Sequence[Any] = (),
) -> pd.DataFrame:
    """職員ごとの早番・遅番・土曜出勤の回数表（:mod:`shiftai.fairness` を利用）。"""
    if result is None or not slots:
        return pd.DataFrame(columns=["職員ID", "氏名"])
    return fairness.report_frame(result, staff, slots, standard=standard, patterns=patterns)


def style_fairness_table(frame: pd.DataFrame, threshold: int = 2) -> Any:
    """偏りの大きいセルを橙、既に大きいセルを赤にする。

    ``threshold`` は「この値を超えたら偏りを疑う」という週内変動の目安。
    """
    if frame.empty:
        return frame

    cols = [c for c in frame.columns if c.endswith("の週内変動")]

    def paint(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        if number <= threshold:
            return ""
        if number >= threshold * 2:
            return f"background-color: {COLOR_SHORTFALL}33; color: {COLOR_SHORTFALL_INK}; font-weight:600;"
        return "background-color: #FB8C0040; color: #8a4b00; font-weight:600;"

    if not cols:
        return frame
    return frame.style.map(paint, subset=cols).format({c: "{:.0f}" for c in cols})


def render_fairness_panel(
    result: SolveResult | None,
    staff: Sequence[Any],
    slots: Sequence[Slot],
    *,
    standard: Any = None,
    patterns: Sequence[Any] = (),
) -> None:
    """公平性レポートを expander の中に描画する（タブ3・タブ4 共通）。"""
    frame = fairness_frame(result, staff, slots, standard=standard, patterns=patterns)
    if frame.empty:
        return
    with st.expander("⚖️ 公平性（早番・遅番・土曜出勤の配分）", expanded=False):
        tally = fairness.counts(result, staff, slots, standard=standard, patterns=patterns)
        stats = [fairness.spread_stats(tally, key) for key in fairness.CATEGORIES]
        metric_row(
            [
                (
                    f"{s.label}の最大差",
                    ("!" if s.max_spread >= 2 else "") + f"{s.max_spread} 回/週",
                    f"最多 {s.max_count} 回 / 最少 {s.min_count} 回",
                )
                for s in stats
            ],
            per_row=3,
        )
        st.dataframe(
            style_fairness_table(frame),
            hide_index=True,
            width="stretch",
            key="fairness_table",
        )
        st.caption(
            "KPI の「◯の最大差」は職員間の偏り（週ごとの最大−最小）、"
            "表の「◯の週内変動」はその職員自身の週ごとのばらつきです。"
            "サイドバーの公平性ペナルティを上げると職員間の偏りが縮みます。"
        )


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


def weekday_pivot_frame(result: SolveResult | None, slots: Sequence[Slot]) -> pd.DataFrame:
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


def staff_hours_frame(result: SolveResult | None, staff: Sequence[Any]) -> pd.DataFrame:
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


def _stat_display(value: object) -> str:
    """``SolveResult.stats`` の 1 値を、表示用の文字列へ正規化する。

    ``stats`` は ``dict``（型引数なし）で、``str`` / ``int`` / ``float`` /
    ``bool`` / ``list`` が同居している（``relaxed_constraints`` と
    ``patterns`` は既定で空のリスト）。

    そのまま値を並べて DataFrame にすると、``pyarrow`` は object 列を
    推論できず ``ArrowInvalid`` を投げ、Streamlit は警告だけ出して
    「automatic fixes」で黙って表示を間違う状態にする
    （実際に「🧾 最適化の詳細」表で毎回発生していた）。
    **列の型が混ざると復元できない**ため、すべてを ``str`` に寄せる。
    """
    if value is None:
        return "—"
    if isinstance(value, set | frozenset):
        # ``set`` の反復順は乱数依存なので、表示が実行ごとに変わらないよう並べる
        value = sorted(value, key=str)
    if isinstance(value, list | tuple | set | frozenset):
        items = [str(v) for v in value]
        return "、".join(items) if items else "（なし）"
    if isinstance(value, bool):
        return "はい" if value else "いいえ"
    # float も ``str()`` に任せる。``f"{value:g}"`` は有効数字 6 桁に丸められて
    # ``202432.35`` が ``202432`` になり、表示として情報量を落とす。
    # ``str(float)`` は最短往復表記なので桁落ちしない。
    return str(value)


def solve_stats_frame(result: SolveResult | None) -> pd.DataFrame:
    """「🧾 最適化の詳細」表（``項目`` / ``値``）を作る。

    ``値`` 列は常に文字列型なので、``stats`` にどんな値が入っていても
    Arrow 変換で失敗しない。
    """
    if result is None:
        return pd.DataFrame()
    # キーでだけ並べる（値が str / list / float と混在しても比較しない）。
    # 「項目」列も ``str`` に寄せる: キーが str と int が混ざっていても
    # object 列のままだと Arrow 変換が壊れる（値列と同じ理由）。
    rows = [
        {"項目": str(key), "値": _stat_display(value)}
        for key, value in sorted((result.stats or {}).items(), key=lambda kv: str(kv[0]))
    ]
    if result.objective_value is not None:
        rows.append({"項目": "目的関数", "値": f"{float(result.objective_value):.3f}"})
    return pd.DataFrame.from_records(rows, columns=["項目", "値"])


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
    lookup = {(a.staff_id, a.day, a.slot.label): a.state for a in result.assignments}
    records = []
    for slot in slots:
        needed = requirements.needed_staff(day, slot)
        needed_q = requirements.needed_qualified(day, slot)
        actual = sum(
            1 for m in staff if lookup.get((m.staff_id, day, slot.label)) is CellState.WORK
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
    age_order = [a.value for a in sorted({r.age_class for r in rows}, key=lambda x: x.sort_key)]
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
            return f"background-color: {COLOR_SHORTFALL}30; color: {COLOR_SHORTFALL_INK}; font-weight:600;"
        if number < 0:
            return f"background-color: {COLOR_OVER}1a; color: {COLOR_OVER_INK};"
        return ""

    style = frame.style.map(_paint, subset=["不足"])
    if "過剰" in frame.columns:
        style = style.map(
            lambda v: (
                f"background-color: {COLOR_OVER}30; color: {COLOR_OVER_INK};"
                if float(v or 0) > 0
                else ""
            ),
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
                "直し方": _first_sentence(_format_hint(fix_hint(v.code))),
            }
            for v in violations
        ]
    )


def _first_sentence(text: str) -> str:
    """テキストの最初の 1 文を返す（「。」で区切る）。"""
    index = text.find("。")
    return text[: index + 1] if index >= 0 else text


FixTarget = tuple[str | None, str | None, str | None]
"""修正対象：``(日付の ISO 文字列, 職員ID, 時間帯ラベル)``。

各要素は ``None`` を取りうる（どれか 1 つしか特定できない違反用）。
欠けている要素は ``None`` で埋める。
"""


def classify_violation(v: Violation) -> str:
    """違反がどの粒度で修正対象にできるかを返す。

    ``cell``: 日付・時間帯・職員ID すべて揃っている（1 つのセルを直す）
    ``slot``: 日付・時間帯は揃っているが職員は特定できない（その列に人員を追加）
    ``staff``: 職員ID は揃っているが日・時間帯は揃っていない（その職員の行を調整）
    ``guide``: 座標が揃っておらず、1 つのセルでは直せない（ガイド文のみ）
    """
    if v.day is not None and v.slot is not None and v.staff_id:
        return "cell"
    if v.day is not None and v.slot is not None:
        return "slot"
    if v.staff_id:
        return "staff"
    return "guide"


def violation_fix_hints() -> dict[str, str]:
    """``gap_analysis.VIOLATION_FIX_HINTS`` があればそれを、無ければ内置の辞書を使う。"""
    found = getattr(gap_analysis, "VIOLATION_FIX_HINTS", None)
    if isinstance(found, dict) and found:
        return {**FALLBACK_VIOLATION_FIX_HINTS, **{str(k): str(v) for k, v in found.items()}}
    return dict(FALLBACK_VIOLATION_FIX_HINTS)


_TAB_KEYS = ("shift", "data", "solve", "export")


def _format_hint(hint: str) -> str:
    """ガイド文中のプレースホルダーをタブ参照文言に展開する。"""
    for key in _TAB_KEYS:
        hint = hint.replace("{" + key + "}", theme.tab_ref_for(key))
    return hint


def fix_hint(code: str) -> str:
    """違反コードの「直し方」を返す。見つからなければ空文字。"""
    return _format_hint(violation_fix_hints().get(str(code), ""))


def partial_violation_targets(
    violations: Sequence[Violation], *, limit: int = 10
) -> list[tuple[str, FixTarget, str]]:
    """セルを 1 つ特定できないが日付や職員は特定できる違反を返す。

    ``(説明文, 修正対象, 粒度)`` の並び。粒度は ``"slot"`` または ``"staff"``。
    上位 ``limit`` 件だけ返す。
    """
    from shiftai.ui import state

    targets: list[tuple[str, FixTarget, str]] = []
    for v in violations:
        level = classify_violation(v)
        if level == "slot":
            assert v.day is not None and v.slot is not None
            target: FixTarget = (v.day.isoformat(), None, v.slot.label)
            label = f"{v.day.isoformat()} {v.slot.label} に人員を追加"
            targets.append((label, target, level))
        elif level == "staff":
            slot_label = None
            slots = (v.detail or {}).get("slots")
            if isinstance(slots, list) and len(slots) == 1:
                slot_label = str(slots[0])
            target = (
                v.day.isoformat() if v.day else None,
                v.staff_id,
                slot_label,
            )
            name = state.staff_name_map().get(v.staff_id, "")
            who = f"{v.staff_id}（{name}）" if name else v.staff_id
            day_part = f"{v.day.isoformat()} " if v.day else ""
            label = f"{day_part}{who} の勤務を調整"
            targets.append((label, target, level))
        if len(targets) >= limit:
            break
    return targets


def unfixable_violations(violations: Sequence[Violation]) -> list[Violation]:
    """1 つのセルにも日付や職員にも特定できない違反を返す。

    日付・時間帯・職員ID のすべてが欠ける違反は、どのセルを変えればよいか
    特定できないため修正ボタンを持たせられない。
    """
    return [v for v in violations if classify_violation(v) == "guide"]


def violation_targets(
    violations: Sequence[Violation], *, limit: int = 20
) -> list[tuple[str, tuple[str, str, str]]]:
    """修正できる違反を ``(説明文, 修正対象)`` の並びにする。

    修正対象は ``(日付の ISO 文字列, 職員ID, 時間帯ラベル)``。
    日付・職員ID・時間帯のどれかが欠けるものは除く（どこを直せばよいか
    特定できないため）。上位 ``limit`` 件だけ返す。
    """
    targets: list[tuple[str, tuple[str, str, str]]] = []
    for v in violations:
        if v.day is None or v.slot is None or not v.staff_id:
            continue
        target = (v.day.isoformat(), v.staff_id, v.slot.label)
        label = f"{v.day.isoformat()} {v.staff_id} {v.slot.label}"
        targets.append((label, target))
        if len(targets) >= limit:
            break
    return targets


def render_violation_actions(violations: Sequence[Violation], *, key_prefix: str) -> None:
    """違反一覧の下に修正ボタンを並べる。

    日付・時間帯・職員ID の揃い方に応じて 3 種類の導線を出す。

    * セル指定可能（3 要素すべて）: セルを選択して直すボタン（現行どおり）
    * 日付・時間帯まで指定可能: その列に人員を追加するボタン
    * 職員まで指定可能: その職員の行を調整するボタン

    押すと ``state.set_fix_target()`` を呼び、**シフト表のタブへ自動で移動する**。
    セルでは直せない違反だけの場合は、違反コードごとの「直し方」ガイドを出す。
    """
    from shiftai.ui import state

    cell_targets = violation_targets(violations)
    partial_targets = partial_violation_targets(violations)
    unfixable = unfixable_violations(violations)
    if not violations:
        return

    def _jump(target: FixTarget) -> None:
        """修正対象を予約し、シフト表のタブを開く。"""
        state.set_fix_target(target)
        st.session_state[state.KEY_FIX_REQUESTED] = True
        theme.goto_tab("shift")

    st.markdown("#### 🛠 この違反を修正する")
    st.caption(
        f"ボタンを押すと、{theme.tab_ref_for('shift')} が開き、該当の日付・職員・時間帯が"
        "選択された状態になります。日付や職員だけが分かる違反には、それぞれの列・行を"
        "指定するボタンが出ます。セルの値を「勤務」「休憩」「オフ」に変えて"
        "「✅ 変更を確定して再最適化する」を押すと、違反が解消されます。"
    )

    shown = cell_targets[:8]
    if shown:
        for start in range(0, len(shown), 2):
            pair = shown[start : start + 2]
            columns = st.columns(len(pair))
            for offset, (label, target) in enumerate(pair):
                index = start + offset
                with columns[offset]:
                    st.button(
                        f"修正 {label}",
                        key=f"{key_prefix}_fix_{index}",
                        width="stretch",
                        help=theme.tab_ref_for("shift") + " を開いて該当セルを編集します。",
                        on_click=_jump,
                        args=(target,),
                    )
        if len(cell_targets) > len(shown):
            st.caption(
                f"ほか {len(cell_targets) - len(shown)} 件のセル指定違反があります。"
            )

    if partial_targets:
        st.markdown(f"##### ⚠️ 日付または職員を選択して修正する（{len(partial_targets)} 件）")
        shown = partial_targets[:8]
        for start in range(0, len(shown), 2):
            pair = shown[start : start + 2]
            columns = st.columns(len(pair))
            for offset, (label, target, level) in enumerate(pair):
                index = start + offset
                icon = "👥" if level == "slot" else "👤"
                with columns[offset]:
                    st.button(
                        f"{icon} {label}",
                        key=f"{key_prefix}_fix_partial_{index}",
                        width="stretch",
                        help=theme.tab_ref_for("shift")
                        + " を開いて該当の列または行を編集します。",
                        on_click=_jump,
                        args=(target,),
                    )
        if len(partial_targets) > len(shown):
            st.caption(
                f"ほか {len(partial_targets) - len(shown)} 件。"
                "下の違反一覧から日付・職員・時間帯を確認してください。"
            )

    if not cell_targets and not partial_targets:
        # セル・部分指定どちらも出ない違反だけの場合はコード別ガイドを出す
        st.warning(
            "この違反は 1 つのセルに特定できないため、修正ボタンを出せません。"
            "違反コードごとの直し方をご確認ください。"
        )
        shown_codes: list[str] = []
        for v in unfixable:
            if v.code in shown_codes:
                continue
            shown_codes.append(v.code)
            hint = fix_hint(v.code) or "園の条件や職員データを見直してください。"
            st.markdown(
                f"- **{violation_title(v.code)}**: {_format_hint(hint)}"
            )
        if len(shown_codes) < len({v.code for v in unfixable}):
            st.caption(
                f"ほか {len({v.code for v in unfixable}) - len(shown_codes)} 種類の違反があります。"
            )

    if unfixable:
        with st.expander(f"ℹ️ セルでは直せない違反（{len(unfixable)} 件）", expanded=True):
            st.dataframe(
                violation_dataframe(unfixable),
                width="stretch",
                hide_index=True,
                key=f"{key_prefix}_unfixable",
            )
            st.caption(
                "「—」は日付・時間帯・職員が特定できないことを表します。"
                "表の「直し方」列、あるいは園の条件・職員データを見直してください。"
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
        with st.expander(
            f"{tone} {severity.value}（{len(group)} 件）",
            expanded=severity is ViolationSeverity.BLOCKER,
        ):
            by_code: dict[str, list[Violation]] = {}
            for violation in group:
                by_code.setdefault(violation.code, []).append(violation)
            ordered = sorted(by_code.items(), key=lambda kv: -len(kv[1]))
            for code, items in ordered:
                st.markdown(f"**{labels.get(code, code)}**（`{code}`） — {len(items)} 件")
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
