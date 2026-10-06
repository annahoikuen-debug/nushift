"""タブ5「出力」: シフト・必要人員・給与計算・Excel・ICAL・ZIP のダウンロード。"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
from datetime import date
from typing import Any

import pandas as pd
import streamlit as st

from shiftai import exporter, gantt_pdf
from shiftai import gas_client as gas
from shiftai.domain import SolveResult, ViolationSeverity
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


def _render_downloads(frames: dict[str, Any], *, enabled: bool = True) -> None:
    """各種ダウンロードボタン。

    ``enabled=False`` のとき実体（``data=``）は生成したままボタンを無効化する。
    既存の試験が実体の存在を検査しているため、短絡評価してはいけない。
    """
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
            disabled=not enabled,
        )
        st.download_button(
            "💴 給与計算 CSV",
            data=exporter.to_csv_bytes(payroll_df),
            file_name=f"{_file_stem('payroll')}.csv",
            mime="text/csv",
            width="stretch",
            key="dl_payroll_csv",
            disabled=not enabled,
        )
        st.download_button(
            "📅 ICAL（カレンダー）",
            data=_ics_bytes(),
            file_name=f"{_file_stem('ics')}.ics",
            mime="text/calendar",
            width="stretch",
            key="dl_ics",
            disabled=not enabled,
        )
    with second:
        st.download_button(
            "🧮 必要人員 CSV",
            data=exporter.to_csv_bytes(requirements_df),
            file_name=f"{_file_stem('requirements')}.csv",
            mime="text/csv",
            width="stretch",
            key="dl_requirements_csv",
            disabled=not enabled,
        )
        st.download_button(
            "📗 Excel ブック（全シート）",
            data=_excel_bytes(frames),
            file_name=f"{_file_stem('workbook')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
            key="dl_excel",
            disabled=not enabled,
        )
        st.download_button(
            "🗜 全文 ZIP",
            data=_zip_bytes(),
            file_name=f"{_file_stem('bundle')}.zip",
            mime="application/zip",
            width="stretch",
            key="dl_zip",
            disabled=not enabled,
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
            disabled=not enabled,
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


def _gantt_pdf_bytes(result: SolveResult, day: date, slots: Any, staff: list[Any]) -> bytes:
    """選択日の日別ガントチャート（A4 縦 1 枚）の PDF。"""
    return gantt_pdf.gantt_pdf_bytes(
        result.day(day),
        slots,
        staff,
        day=day,
        fixed=state.normalize_fixed(state.get(state.KEY_FIXED_ASSIGNMENTS)),
        facility_name=state.current_settings().facility_name,
    )


def _render_gantt_pdf(result: SolveResult, *, enabled: bool = True) -> None:
    """日別ガントチャートの PDF ダウンロード。

    A4 は 1 日 1 枚なので、対象日をこちらで選ばせる（タブ4 の選択と独立）。
    他の出力と同じく「出力前の確認」に未達なら無効化する。実体は生成したままにする。
    """
    st.markdown("#### 📄 日別ガントチャート（A4 1 枚）")
    options = components.day_options(result)
    if not options:
        st.info("出力できる日がありません。")
        return
    slots = state.current_slots()
    staff = state.get(state.KEY_STAFF) or []
    if not staff or not slots:
        st.error("職員データまたは時間帯が未設定です。")
        return
    day = st.selectbox(
        "出力対象の日付",
        options=options,
        index=0,
        format_func=theme.format_day,
        key="gantt_pdf_day",
    )
    st.caption(
        "選択した日のみ、A4 縦 1 枚に印刷します（職員全員が 1 ページに収まります）。"
        "緑＝勤務、橙＝休憩、紫枠＝手動確定のセル。"
    )
    st.download_button(
        "⬇️ ガントチャート PDF（A4）",
        data=_gantt_pdf_bytes(result, day, slots, staff),
        file_name=f"{_file_stem('gantt')}_{day.isoformat()}.pdf",
        mime="application/pdf",
        width="stretch",
        key="dl_gantt_pdf",
        disabled=not enabled,
    )


@dataclasses.dataclass(frozen=True)
class ChecklistItem:
    """出力前の確認 1 項目。``ok`` が偽ならダウンロードを止める理由になる。"""

    label: str
    ok: bool
    detail: str
    action: str
    """未達だったときに行うこと（1 文）。"""


def _checklist_items(result: SolveResult) -> list[ChecklistItem]:
    """出力前の確認項目を判定して返す。

    判定そのものは描画から切り離し、未達項目ごとの ``action`` を
    持ったまま利用できるようにする。
    """
    report = state.get(state.KEY_GAP_REPORT)
    violations = state.apply_acknowledgements(state.get(state.KEY_VIOLATIONS) or [])
    blockers = result.blockers()
    shortfall = report.total_shortfall_slots if report else 0
    status_value = getattr(result.status, "value", str(result.status))
    # 未確認の要調整項目だけを数える。参考（INFO）はブロック要因にしない。
    # 修正前: ``not violations``（INFO を含む全件）だったため、
    # ``HOURS_IMBALANCE`` が 1 件あるだけで全ダウンロードが無効になっていた。
    warnings = [v for v in violations if v.severity is ViolationSeverity.WARNING]
    pending = [v for v in warnings if not v.acknowledged]
    infos = [v for v in violations if v.severity is ViolationSeverity.INFO]
    return [
        ChecklistItem(
            "法令違反（BLOCKER）が 0 件",
            not blockers,
            f"{len(blockers)} 件",
            "下の「この違反を修正する」の修正ボタンから、各セルの値を直してください。",
        ),
        ChecklistItem(
            "配置不足時間帯が 0 件",
            bool(report and shortfall == 0),
            f"{shortfall} 件",
            f"{theme.tab_ref_for('shift')} で不足している時間帯に職員を追加するか、"
            f"{theme.tab_ref_for('requirements')} で基準・人員を見直してください。",
        ),
        ChecklistItem(
            "最適化状態が「最適解」または「実行可能解」",
            result.ok,
            status_value,
            f"{theme.tab_ref_for('solve')} に戻り、入力を調整してから"
            "「🚀 シフトを自動作成する」を押し直してください。",
        ),
        ChecklistItem(
            "要調整項目を確認済み",
            not pending,
            (
                f"未確認 {len(pending)} 件 / 全 {len(warnings)} 件"
                f"（参考 {len(infos)} 件は出力に影響しません）"
            ),
            "下の違反一覧（深刻度別）で内容を確認し、必要ならセルの値を直すか、"
            "「確認済みにする」を押してください。"
            if pending
            else "",
        ),
    ]


def _render_checklist(result: SolveResult) -> bool:
    """出力前の最終確認リスト。すべて満たしていれば真を返す。

    1 つでも満たしていなければダウンロードボタンを無効化する。
    未達項目は「何が未達か」と「次に何を押すか」を 1 行ずつ対で示す。
    """
    items = _checklist_items(result)
    st.markdown("#### ✅ 出力前の確認")
    for item in items:
        st.markdown(f"- {'✅' if item.ok else '⚠️'} {item.label}（現在: {item.detail}）")
        if not item.ok and item.action:
            st.markdown(f"　↳ **次の一手**: {item.action}")
    st.caption(
        "※ チェックを満たしていない場合はダウンロードできません。"
        "実際の運用では園長・設置責任者が必ず内容を確認してください。"
    )
    return all(item.ok for item in items)


def _render_blocked_help(blocked: bool) -> None:
    """ダウンロードが無効なときに「次に何を押すか」まで示す。

    以前は「違反を直してください」とだけ言って終了しており、
    どこへ移動すればよいかも分からず、タブを跨いだ操作もできなかった。
    """
    if not blocked:
        return
    st.error(
        "出力前の確認に未達があるため、ダウンロードできません。"
        "下の「🚨 ダウンロードを解除する手順」を 1 番から順に実行してください。"
    )
    st.markdown("##### 🚨 ダウンロードを解除する手順")
    steps: list[tuple[str, str, str]] = [
        (
            "1",
            "violations",
            f"{theme.tab_ref_for('shift')} を開き、「🛠 この違反を修正する」"
            "のボタンで該当セルを選びます。",
        ),
        (
            "2",
            "edit",
            "「✏️ 微調整」で値を「勤務」「休憩」「オフ」に変更し、"
            "「✅ 変更を確定して再最適化」を押します（押さないと反映されません）。",
        ),
        (
            "3",
            "data",
            f"セルでは直せない違反（過不足・労働時間など）が残る場合は "
            f"{theme.tab_ref_for('data')} の入力、または "
            f"{theme.tab_ref_for('solve')} の再計算で解消します。",
        ),
        (
            "4",
            "back",
            f"以上が終わったら {theme.tab_ref_for('export')}（このタブ）に"
            "戻ると、✅ が付きダウンロードが有効になります。",
        ),
    ]
    for order, target, text in steps:
        st.markdown(f"{order}. {text}")
        if target == "violations":
            st.button(
                f"🔧 {theme.tab_ref_for('shift')} で違反を修正する",
                key="export_help_fix",
                width="stretch",
                type="primary",
                on_click=theme.goto_tab,
                args=("shift",),
            )
        elif target == "data":
            columns = st.columns(2)
            with columns[0]:
                st.button(
                    f"📥 {theme.tab_ref_for('data')} を開く",
                    key="export_help_data",
                    width="stretch",
                    on_click=theme.goto_tab,
                    args=("data",),
                )
            with columns[1]:
                st.button(
                    f"🚀 {theme.tab_ref_for('solve')} で作り直す",
                    key="export_help_solve",
                    width="stretch",
                    on_click=theme.goto_tab,
                    args=("solve",),
                )
    st.caption(
        "修正の途中で「⚠️ 再計算が必要です」と表示されたときは、"
        "入力条件が計算後に変わっています。"
        "推測で進めず、条件を確認してから再度「🚀 シフトを自動作成する」を実行してください。"
    )


def _render_acknowledge_control(violations: list) -> None:
    """要調整項目をまとめて「確認済み」にする操作を提供する。

    修正前は「参考（INFO）」を含む全違反の件数をゲートにしていたため、
    ``HOURS_IMBALANCE`` が 1 件あるだけで全ダウンロードが無効になり、
    解除する手段がなかった。ここでは要調整（WARNING）だけを列挙し、
    確認済みにする／戻すadion_operationを提供する。法令違反（BLOCKER）は
    確認済みでは解除できない。
    """
    warnings = [
        v for v in violations if v.severity is ViolationSeverity.WARNING and not v.acknowledged
    ]
    if not warnings:
        return

    with st.expander(
        f"👀 未確認の要調整項目を確認する（{len(warnings)} 件）",
        expanded=False,
    ):
        st.caption(
            "内容を確認し、そのままで運用できると判断した項目は"
            "「確認済みにする」を押してください。"
            "セルの修正が必要な項目は、 shift タブの「この違反を修正する」を使います。"
        )
        labels = [f"{v.code}｜{v.staff_id or '園全体'}｜{v.message}" for v in warnings]
        selected = st.multiselect(
            "確認済みにする項目",
            options=labels,
            key="export_ack_select",
            width="stretch",
        )
        columns = st.columns(2)
        with columns[0]:
            if st.button(
                "✅ 選択項目を確認済みにする",
                key="export_ack_apply",
                width="stretch",
                disabled=not selected,
            ):
                marks = set(state.acknowledged_fingerprints())
                chosen = set(selected)
                marks.update(
                    v.fingerprint
                    for v, label in zip(warnings, labels, strict=True)
                    if label in chosen
                )
                state.set_acknowledged(marks)
                st.rerun()
        with columns[1]:
            if st.button(
                "↩︎ すべての確認済みを取り消す",
                key="export_ack_clear",
                width="stretch",
                disabled=not state.acknowledged_fingerprints(),
            ):
                state.set_acknowledged(())
                st.rerun()


def render() -> None:
    """タブ5 の本体。"""
    theme.step_indicator(4)
    theme.heading(4)
    result = state.get(state.KEY_SOLVE_RESULT)
    if result is None or not result.shift_days:
        theme.empty_state(f"まず{theme.tab_ref(2)}（シフト作成）でシフトを作成してください。")
        return
    staff = state.get(state.KEY_STAFF) or []
    slots = state.current_slots()
    if not staff or not slots:
        st.error("職員データまたは時間帯が未設定です。")
        theme.stuck_hint(4, "職員データまたは時間帯が未設定です。")
        return

    checklist_ok = _render_checklist(result)
    raw_violations = state.get(state.KEY_VIOLATIONS) or []
    # 確認済み状態を反映してから表示する（チェックリストと一覧が食い違わないように）。
    violations = state.apply_acknowledgements(raw_violations)
    components.render_violations(violations)
    _render_acknowledge_control(raw_violations)
    components.render_violation_actions(violations, key_prefix="export")
    _render_blocked_help(not checklist_ok)
    st.divider()
    try:
        frames = _frames(result, slots, staff)
        _render_preview(frames)
        st.divider()
        _render_downloads(frames, enabled=checklist_ok)
    except Exception as exc:  # noqa: BLE001 - 出力失敗で画面を落とさない
        st.error(f"出力データの生成に失敗しました: {exc}")
        return
    st.divider()
    _render_gantt_pdf(result, enabled=checklist_ok)
    st.divider()
    _render_gas_push(result)
    theme.caveat_box()
    theme.next_step_hint(4)
