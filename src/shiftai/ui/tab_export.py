"""タブ5「出力」: シフト・必要人員・給与計算・Excel・ICAL・ZIP のダウンロード。"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

import pandas as pd
import streamlit as st

from shiftai import exporter
from shiftai import gas_client as gas
from shiftai.domain import SolveResult
from shiftai.ui import components, state, theme

PREVIEW_TABLES: tuple[tuple[str, str], ...] = (
    ("shift", "🗓 シフト（1セル1行）"),
    ("requirements", "🧮 必要人員"),
    ("payroll", "💴 給与計算"),
    ("matrix", "🧱 シフトマトリクス"),
)

# ファイル名に使えない文字。日本語（園名）は残したいので、
# ASCII のみを対象に「空白・区切り文字・パス区切り」を落とす。
_UNSAFE = re.compile(r"[\s\\/:*?\"<>|\x00-\x1f]+")
_ASCII_UNSAFE = re.compile(r"[^0-9A-Za-z_.\-぀-ヿ一-鿿]+")


def _safe_name(text: str) -> str:
    """ファイル名に使える形へ整形する。

    園名は日本語であることが多いので、日本語はそのまま残す。
    以前は ASCII のみ許可していたため、既定の園名「あさひ保育園」が
    すべて削られ、全ダウンロードのファイル名が
    ``シフト_shiftai_<期間>_<種類>.<拡張子>`` になっていた。
    """
    cleaned = _UNSAFE.sub("_", unicodedata.normalize("NFC", str(text))).strip("_")
    cleaned = _ASCII_UNSAFE.sub("_", cleaned).strip("_")
    return cleaned or "shiftai"


def _file_stem(extension: str) -> str:
    """園名と日付範囲を含むファイル名の stem を作る。"""
    settings = state.current_settings()
    days = components.days_of(state.get(state.KEY_SOLVE_RESULT)) or state.current_days()
    facility = _safe_name(settings.facility_name)
    if days:
        period = f"{min(days).isoformat()}_{max(days).isoformat()}"
    else:
        period = date.today().isoformat()
    return f"シフト_{facility}_{period}_{extension}"


def _frames(result: SolveResult, slots: Any, staff: list[Any]) -> dict[str, Any]:
    """出力用の DataFrame をまとめて作る。"""
    table = state.get(state.KEY_REQUIREMENTS)
    settings = state.current_settings()
    return {
        "shift": exporter.shift_to_dataframe(result, slots, staff),
        "requirements": (exporter.requirements_dataframe(table) if table is not None else None),
        "payroll": exporter.payroll_dataframe(result, slots, staff, settings),
        "matrix": exporter.shift_matrices(result, slots, staff),
    }


def _render_preview(frames: dict[str, Any]) -> None:
    """出力内容のプレビュー。"""
    st.markdown("#### 👁 出力内容のプレビュー")
    for key, label in PREVIEW_TABLES:
        frame = frames.get(key)
        with st.expander(
            f"{label}（{0 if frame is None else len(frame)} 行）", expanded=key == "shift"
        ):
            if frame is None or frame.empty:
                st.info("この出力は生成できません。")
                continue
            st.dataframe(frame.head(200), hide_index=True, width="stretch", key=f"preview_{key}")
            if len(frame) > 200:
                st.caption(f"先頭 200 行を表示しています（全 {len(frame)} 行）。")


def _render_downloads(frames: dict[str, Any]) -> None:
    """各種ダウンロードボタン。"""
    st.markdown("#### ⬇️ ダウンロード")
    st.caption(
        "CSV は Excel 互換のため UTF-8 BOM 付きで出力します。"
        "ICAL は各勤務ブロックを 1 イベントとして書き出します（Google カレンダーなどに読ませられます）。"
    )
    shift_df = frames["shift"]
    requirements_df = frames["requirements"]
    payroll_df = frames["payroll"]
    if requirements_df is None:
        requirements_df = pd.DataFrame(columns=exporter.REQUIREMENT_DF_COLUMNS)

    first, second = st.columns(2)
    with first:
        st.download_button(
            "🗓 シフト CSV",
            data=exporter.to_csv_bytes(shift_df),
            file_name=f"{_file_stem('shift')}.csv",
            mime="text/csv",
            width="stretch",
            key="dl_shift_csv",
        )
        st.download_button(
            "💴 給与計算 CSV",
            data=exporter.to_csv_bytes(payroll_df),
            file_name=f"{_file_stem('payroll')}.csv",
            mime="text/csv",
            width="stretch",
            key="dl_payroll_csv",
        )
        st.download_button(
            "📅 ICAL（カレンダー）",
            data=_ics_bytes(),
            file_name=f"{_file_stem('ics')}.ics",
            mime="text/calendar",
            width="stretch",
            key="dl_ics",
        )
    with second:
        st.download_button(
            "🧮 必要人員 CSV",
            data=exporter.to_csv_bytes(requirements_df),
            file_name=f"{_file_stem('requirements')}.csv",
            mime="text/csv",
            width="stretch",
            key="dl_requirements_csv",
        )
        st.download_button(
            "📗 Excel ブック（全シート）",
            data=_excel_bytes(frames),
            file_name=f"{_file_stem('workbook')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
            key="dl_excel",
        )
        st.download_button(
            "🗜 全文 ZIP",
            data=_zip_bytes(),
            file_name=f"{_file_stem('bundle')}.zip",
            mime="application/zip",
            width="stretch",
            key="dl_zip",
        )

    with st.expander("📄 サマリ（Markdown）", expanded=False):
        markdown = _summary_markdown()
        st.markdown(markdown)
        st.download_button(
            "⬇️ サマリ Markdown をダウンロード",
            data=markdown.encode("utf-8"),
            file_name=f"{_file_stem('summary')}.md",
            mime="text/markdown",
            key="dl_summary",
        )


def _ics_bytes() -> bytes:
    result = state.get(state.KEY_SOLVE_RESULT)
    slots = state.current_slots()
    staff = state.get(state.KEY_STAFF) or []
    return exporter.to_ics(result, slots, staff).encode("utf-8")


def _excel_bytes(frames: dict[str, Any]) -> bytes:
    sheets = {
        "シフト": frames["shift"],
        "必要人員": frames["requirements"],
        "給与計算": frames["payroll"],
        "マトリクス": frames["matrix"],
    }
    sheets = {name: frame for name, frame in sheets.items() if frame is not None}
    return exporter.to_excel_bytes(sheets)


def _zip_bytes() -> bytes:
    result = state.get(state.KEY_SOLVE_RESULT)
    table = state.get(state.KEY_REQUIREMENTS)
    slots = state.current_slots()
    staff = state.get(state.KEY_STAFF) or []
    return exporter.export_bundle_zip(
        result,
        table,
        slots,
        staff,
        state.current_settings(),
        gap_report=state.get(state.KEY_GAP_REPORT),
        violations=state.get(state.KEY_VIOLATIONS) or [],
    )


def _summary_markdown() -> str:
    result = state.get(state.KEY_SOLVE_RESULT)
    slots = state.current_slots()
    staff = state.get(state.KEY_STAFF) or []
    settings = state.current_settings()
    days = components.days_of(result)
    period = (min(days), max(days)) if days else None
    return exporter.summary_markdown(
        result,
        slots,
        staff,
        facility_name=settings.facility_name,
        period=period,
    )


def _render_gas_push(result: SolveResult) -> None:
    """GAS への push ボタン（設定されている場合のみ）。"""
    if not gas.available():
        return
    st.markdown("#### 📡 Google スプレッドシートへ送信")
    config = gas.GasConfig.from_env()
    st.caption(
        f"接続先: {config.base_url if config else '—'}"
        "（送信は「置き換え」方式です。シートの内容は上書きされます。）"
    )
    if not st.button(
        "📤 シフトをシートへ送信",
        key="gas_push_shift",
        width="stretch",
    ):
        return
    slots = state.current_slots()
    staff = state.get(state.KEY_STAFF) or []
    try:
        frame = exporter.shift_to_dataframe(result, slots, staff)
        client = gas.GoogleAppsScriptClient(gas.GasConfig.from_env())
        state.set(state.KEY_GAS_CLIENT, client)
        client.push_shift(frame)
        st.success(f"{len(frame)} 行を送信しました。")
    except gas.GasError as exc:
        st.error(f"送信に失敗しました: {exc}")
    except gas.GasConfigError as exc:
        st.error(f"GAS の設定が不正です: {exc}")
    except Exception as exc:  # noqa: BLE001 - GAS 連携で画面を落とさない
        st.error(f"送信に予期しないエラーが発生しました: {exc}")


def _render_checklist() -> None:
    """出力前の最終確認リスト。"""
    result = state.get(state.KEY_SOLVE_RESULT)
    report = state.get(state.KEY_GAP_REPORT)
    violations = state.get(state.KEY_VIOLATIONS) or []
    st.markdown("#### ✅ 出力前の確認")
    items = []
    items.append(
        ("法令違反（BLOCKER）が 0 件", not result.blockers(), f"{len(result.blockers())} 件")
    )
    items.append(
        (
            "配置不足時間帯が 0 件",
            bool(report and report.total_shortfall_slots == 0),
            f"{report.total_shortfall_slots if report else 0} 件",
        )
    )
    items.append(
        (
            "最適化状態が「最適解」または「実行可能解」",
            result.ok,
            getattr(result.status, "value", str(result.status)),
        )
    )
    items.append(("要調整項目を確認済み", not violations, f"{len(violations)} 件"))
    for label, ok, detail in items:
        st.markdown(f"- {'✅' if ok else '⚠️'} {label}（現在: {detail}）")
    st.caption(
        "※ チェックは表示のみです。実際の運用では園長・設置責任者が必ず内容を確認してください。"
    )


def render() -> None:
    """タブ5 の本体。"""
    theme.step_indicator(4)
    st.markdown("### 5. 出力")
    result = state.get(state.KEY_SOLVE_RESULT)
    if result is None or not result.shift_days:
        theme.empty_state("まずタブ3「シフト作成」でシフトを作成してください。")
        return
    staff = state.get(state.KEY_STAFF) or []
    slots = state.current_slots()
    if not staff or not slots:
        st.error("職員データまたは時間帯が未設定です。")
        return

    _render_checklist()
    st.divider()
    try:
        frames = _frames(result, slots, staff)
        _render_preview(frames)
        st.divider()
        _render_downloads(frames)
    except Exception as exc:  # noqa: BLE001 - 出力失敗で画面を落とさない
        st.error(f"出力データの生成に失敗しました: {exc}")
        return
    st.divider()
    _render_gas_push(result)
    theme.caveat_box()
    theme.next_step_hint(4)
