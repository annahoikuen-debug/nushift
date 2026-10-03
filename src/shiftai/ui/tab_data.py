"""タブ1「データ投入」: 園児（登降園予定）・職員・希望休の読み込みと直接編集。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pandas as pd
import streamlit as st

from shiftai import data_loader, importers, sample_data
from shiftai.config import APP_TIME_INPUT_STEP_SECONDS
from shiftai.domain import AgeClass, EmploymentType, Role
from shiftai.ui import components, state, theme
from shiftai.validation import ERROR, ValidationReport, validate_frames

TABLE_KINDS: tuple[tuple[str, str, str, str], ...] = (
    (
        "children",
        "🧒 園児（登降園予定）",
        "ℹ️ この表の読み方",
        "<p>園児が 1 プラン 1 行。短時間保育・早朝保育・延長保育のフラグで"
        "その日の必要人員が変わります。</p>",
    ),
    (
        "staff",
        "👩‍🏫 職員",
        "ℹ️ この表の読み方",
        "<p>契約時間がそのまま「供給できる人時」になります。"
        "週契約時間を増やさないと基準を満たせません。</p>",
    ),
    (
        "preferences",
        "🌴 希望休",
        "ℹ️ この表の読み方",
        "<p>「出勤不可」「希望休」はハード制約（シフトを割り当てません）。"
        "それ以外はソフト制約です。</p>",
    ),
)
"""(内部キー, 見出し, ツールチップのラベル, ツールチップ本文) の並び。

説明は初期画面で常に目に入るグレーのキャプションにせず、``theme.tip`` の
ツールチップへ入れる。``table_tip`` が「想定される列」を自動で足すため、
本文には列一覧を書かない（``data_loader.TABLE_COLUMNS`` と二重に管理しない）。
"""

YES_NO: tuple[str, str] = ("true", "false")

COLUMN_KINDS: dict[str, dict[str, str]] = {
    "children": {
        "園児ID": "text",
        "氏名": "text",
        "年齢": "text",
        "登園日": "date",
        "登園時刻": "time",
        "降園時刻": "time",
        "短時間保育": "text",
        "欠席": "text",
        "欠席理由": "text",
        "早朝保育": "text",
        "延長保育": "text",
        "備考": "text",
    },
    "staff": {
        "職員ID": "text",
        "氏名": "text",
        "資格（主）": "text",
        "資格（副）": "text",
        "雇用形態": "text",
        "週契約時間": "number",
        "1日契約時間": "number",
        "月間最小時間": "number",
        "月間最大時間": "number",
        "週最大出勤日数": "int",
        "最大連続勤務日数": "int",
        "最早始業": "time",
        "最遅終業": "time",
        "能力タグ": "text",
        "備考": "text",
    },
    "preferences": {
        "職員ID": "text",
        "種別": "text",
        "日付": "date",
        "開始": "time",
        "終了": "time",
        "理由": "text",
    },
}


def coerce_frame(kind: str, frame: pd.DataFrame) -> pd.DataFrame:
    """``st.data_editor`` が要求する型（date / time / number）へ変換する。

    ``data_loader.parse_date`` / ``parse_time`` は ``date`` / ``time`` オブジェクトを
    そのまま受け付けるので、変換後の値をそのまま ``load_bundle`` に渡せる。
    """
    if frame is None or frame.empty:
        return frame
    result = frame.copy()
    for column, column_kind in COLUMN_KINDS.get(kind, {}).items():
        if column not in result.columns:
            continue
        series = result[column]
        if column_kind == "date":
            result[column] = pd.to_datetime(series, errors="coerce")
        elif column_kind == "time":
            result[column] = series.map(data_loader.parse_time)
        elif column_kind in ("number", "int"):
            result[column] = pd.to_numeric(series, errors="coerce")
        else:
            result[column] = series.map(lambda v: "" if pd.isna(v) else str(v))
    return result


def column_config(kind: str) -> dict[str, Any]:
    """``st.data_editor`` 用の日本語 ``column_config``。"""
    if kind == "children":
        return {
            "園児ID": st.column_config.TextColumn("園児ID", required=True, width="small"),
            "氏名": st.column_config.TextColumn("氏名", required=True),
            "年齢": st.column_config.SelectboxColumn(
                "年齢",
                options=[str(a.years) for a in AgeClass],
                required=True,
                help="0歳児〜5歳児以上。定員比と必要人員の基準になります。",
            ),
            "登園日": st.column_config.DateColumn("登園日", format="YYYY/MM/DD", required=True),
            "登園時刻": st.column_config.TimeColumn(
                "登園時刻",
                format="HH:MM",
                step=APP_TIME_INPUT_STEP_SECONDS,
                help="この時刻より前は在園児として数えません。",
            ),
            "降園時刻": st.column_config.TimeColumn(
                "降園時刻",
                format="HH:MM",
                step=APP_TIME_INPUT_STEP_SECONDS,
                help="この時刻以降は在園児として数えません。",
            ),
            "短時間保育": st.column_config.SelectboxColumn(
                "短時間保育",
                options=list(YES_NO),
                help="true で短時間保育（定時保育）の扱いになります。",
            ),
            "欠席": st.column_config.SelectboxColumn(
                "欠席",
                options=list(YES_NO),
                help="true でその日の在園児から除外します（登降園時刻は不要）。",
            ),
            "欠席理由": st.column_config.TextColumn(
                "欠席理由", help="欠席したときの記録用。集計には影響しません。"
            ),
            "早朝保育": st.column_config.SelectboxColumn(
                "早朝保育",
                options=list(YES_NO),
                help="true で早朝保育時間帯の在園児として数えます。",
            ),
            "延長保育": st.column_config.SelectboxColumn(
                "延長保育",
                options=list(YES_NO),
                help="true で延長保育時間帯の在園児として数えます。",
            ),
            "備考": st.column_config.TextColumn("備考", help="自由記入。集計には影響しません。"),
        }
    if kind == "staff":
        return {
            "職員ID": st.column_config.TextColumn("職員ID", required=True, width="small"),
            "氏名": st.column_config.TextColumn("氏名", required=True),
            "資格（主）": st.column_config.SelectboxColumn(
                "資格（主）", options=[r.value for r in Role], required=True
            ),
            "資格（副）": st.column_config.TextColumn(
                "資格（副）", help="複数のときは | で区切ります（例: 保育士|看護師）"
            ),
            "雇用形態": st.column_config.SelectboxColumn(
                "雇用形態", options=[e.value for e in EmploymentType]
            ),
            "週契約時間": st.column_config.NumberColumn(
                "週契約時間", min_value=0.0, max_value=80.0, step=0.5, format="%.1f"
            ),
            "1日契約時間": st.column_config.NumberColumn(
                "1日契約時間", min_value=0.5, max_value=14.0, step=0.5, format="%.1f"
            ),
            "月間最小時間": st.column_config.NumberColumn(
                "月間最小時間", min_value=0.0, max_value=400.0, step=1.0
            ),
            "月間最大時間": st.column_config.NumberColumn(
                "月間最大時間", min_value=0.0, max_value=400.0, step=1.0
            ),
            "週最大出勤日数": st.column_config.NumberColumn(
                "週最大出勤日数", min_value=0, max_value=7, step=1, format="%d"
            ),
            "最大連続勤務日数": st.column_config.NumberColumn(
                "最大連続勤務日数", min_value=1, max_value=7, step=1, format="%d"
            ),
            "最早始業": st.column_config.TimeColumn(
                "最早始業", format="HH:MM", step=APP_TIME_INPUT_STEP_SECONDS
            ),
            "最遅終業": st.column_config.TimeColumn(
                "最遅終業", format="HH:MM", step=APP_TIME_INPUT_STEP_SECONDS
            ),
            "能力タグ": st.column_config.TextColumn(
                "能力タグ", help="| で区切ります（例: 乳幼児研修修了|ピアノ指導可）"
            ),
            "備考": st.column_config.TextColumn("備考"),
        }
    return {
        "職員ID": st.column_config.TextColumn("職員ID", required=True, width="small"),
        "種別": st.column_config.SelectboxColumn(
            "種別",
            options=["出勤不可", "希望休", "休み希望", "出勤希望"],
            required=True,
            help="出勤不可・希望休はハード制約です。",
        ),
        "日付": st.column_config.DateColumn("日付", format="YYYY/MM/DD", required=True),
        "開始": st.column_config.TimeColumn(
            "開始", format="HH:MM", step=APP_TIME_INPUT_STEP_SECONDS
        ),
        "終了": st.column_config.TimeColumn(
            "終了", format="HH:MM", step=APP_TIME_INPUT_STEP_SECONDS
        ),
        "理由": st.column_config.TextColumn("理由"),
    }


def table_tip(kind: str, label: str, detail: str) -> None:
    """表の説明をツールチップへ閉じ込める（hover / クリックで開く）。

    初期画面を短く保つため、説明と「想定される列」は常時表示にしない。
    列一覧は ``data_loader.TABLE_COLUMNS`` から組み立てるので、表の定義と
    説明文が二重管理されることがない。
    """
    columns = escape(" / ".join(data_loader.TABLE_COLUMNS[kind]))
    theme.tip(label, f"{detail}<p><b>想定される列</b>: {columns}</p>")


def _seed_for_table() -> int:
    return int(st.session_state.get("sample_seed", 42))


def sync_seed_inputs(master: int) -> None:
    """共通シードを変えたら、各表のシード入力を既定値に戻す。"""
    previous = st.session_state.get("sample_seed_applied", master)
    if previous == master:
        return
    for kind, _title, _label, _detail in TABLE_KINDS:
        st.session_state.pop(f"sample_seed_{kind}", None)
    st.session_state["sample_seed_applied"] = master


def load_sample(kind: str, days: Sequence[date], seed: int) -> pd.DataFrame:
    """``sample_data`` から該当テーブルの DataFrame を作る。"""
    children, staff, preferences = sample_data.make_dataset(days, seed=seed)
    frames = sample_data.sample_dataframes(children=children, staff=staff, preferences=preferences)
    return frames[kind]


def read_uploads(kind: str, files: Sequence[Any]) -> list[pd.DataFrame]:
    """アップロードされたファイルを DataFrame として読む。"""
    frames: list[pd.DataFrame] = []
    for uploaded in files:
        raw = uploaded.getvalue() if hasattr(uploaded, "getvalue") else uploaded
        frames.append(data_loader.read_table(raw, kind))
    return frames


def _history_label(kind: str, title: str) -> str:
    """履歴へ記録するときの表示名（例: 「園児 42 行」）。"""
    frame = st.session_state.get(f"frame_{kind}", None)
    rows = 0 if frame is None else len(frame)
    return f"{title} {rows} 行"


def _record_edit(kind: str, frame: pd.DataFrame) -> None:
    """編集内容を履歴へ記録する（同一内容は取り込まない）。"""
    state.edit_history(kind).record(kind, frame, _history_label(kind, table_title(kind)))


def store_frame(kind: str, frame: pd.DataFrame, *, reset_editor: bool = True) -> pd.DataFrame:
    """編集内容を ``session_state`` へ保存する（型変換・履歴記録つき）。

    ``reset_editor=False`` は、同じ実行内で直後に ``st.data_editor`` を
    描画するとき用。**ウィジェットを生成したあとでキー ``editor_<kind>`` を
    書き換えると Streamlit が例外を投げる**ため、エディタより前に評価される
    場面（サンプル読み込み・アップロード）では ``reset_editor=True`` のまま使う。
    """
    coerced = coerce_frame(kind, frame)
    _record_edit(kind, coerced)
    st.session_state[f"frame_{kind}"] = coerced
    if reset_editor:
        st.session_state.pop(f"editor_{kind}", None)
    return coerced


def editor_frame(kind: str) -> pd.DataFrame:
    """``st.data_editor`` に出すフレーム（未投入なら空テンプレート）。"""
    frame = st.session_state.get(f"frame_{kind}", None)
    if frame is None or frame.empty:
        return pd.DataFrame(columns=data_loader.TABLE_COLUMNS[kind])
    return coerce_frame(kind, frame)


def frame_rows(kind: str) -> int:
    """その表が何行あるか（未投入・空なら 0）。"""
    frame = st.session_state.get(f"frame_{kind}", None)
    return 0 if frame is None else len(frame)


def table_title(kind: str) -> str:
    for _kind, title, _label, _detail in TABLE_KINDS:
        if _kind == kind:
            return title
    return kind


def table_tip_body(kind: str) -> tuple[str, str]:
    """その表のツールチップ（ラベル, 本文）を返す。"""
    for _kind, _title, label, detail in TABLE_KINDS:
        if _kind == kind:
            return label, detail
    return "ℹ️ この表の読み方", "<p></p>"


def _render_history_controls(kind: str) -> None:
    """Undo / Redo ボタンと履歴のキャプションを描く。"""
    history = state.edit_history(kind)
    cols = st.columns([1, 1, 2])
    with cols[0]:
        if st.button(
            f"↩︎ {history.undo_label()}",
            disabled=not history.can_undo,
            key=f"undo_{kind}",
            width="stretch",
            help="直前の編集内容に戻します。",
        ):
            entry = history.undo()
            if entry is not None:
                st.session_state[f"frame_{kind}"] = entry.frame.copy()
                st.session_state.pop(f"editor_{kind}", None)
                st.rerun()
    with cols[1]:
        if st.button(
            f"↪︎ {history.redo_label()}",
            disabled=not history.can_redo,
            key=f"redo_{kind}",
            width="stretch",
            help="元に戻した編集をもう一度適用します。",
        ):
            entry = history.redo()
            if entry is not None:
                st.session_state[f"frame_{kind}"] = entry.frame.copy()
                st.session_state.pop(f"editor_{kind}", None)
                st.rerun()
    with cols[2]:
        st.caption(history.trail())


def _preview_table(kind: str, title: str, label: str, detail: str) -> None:
    columns = data_loader.TABLE_COLUMNS[kind]
    days = state.current_days()
    table_tip(kind, label, detail)

    seed_row, template_col, load_col = st.columns([1, 1, 1])
    with seed_row:
        st.number_input(
            "乱数シード",
            min_value=0,
            max_value=9999,
            value=_seed_for_table(),
            step=1,
            key=f"sample_seed_{kind}",
            help="同じシードなら同じデータが入ります。",
        )
    with template_col:
        if st.button(
            "テンプレートを出力（1行サンプルの CSV）",
            key=f"template_{kind}",
            width="stretch",
        ):
            _offer_templates(kind, title)
    with load_col:
        if st.button(
            "サンプルデータを読み込む",
            key=f"sample_{kind}",
            type="primary" if kind == "staff" else "secondary",
            width="stretch",
        ):
            frame = load_sample(kind, days, int(st.session_state.get(f"sample_seed_{kind}", 42)))
            st.session_state[f"frame_{kind}"] = frame
            st.session_state.pop(f"editor_{kind}", None)
            _record_edit(kind, frame)
            st.session_state[f"seed_{kind}"] = int(st.session_state.get(f"sample_seed_{kind}", 42))
            st.toast(f"{title} のサンプルを {len(frame)} 行読み込みました", icon="✅")

    uploads = st.file_uploader(
        f"{title} のファイル（CSV / Excel）",
        type=["csv", "xlsx"],
        accept_multiple_files=True,
        key=f"upload_{kind}",
    )
    if uploads:
        try:
            frames = read_uploads(kind, uploads)
            merged = pd.concat(frames, ignore_index=True)
            st.session_state[f"frame_{kind}"] = merged
            st.session_state.pop(f"editor_{kind}", None)
            _record_edit(kind, merged)
            st.success(f"{len(merged)} 行を読み込みました。")
        except Exception as exc:  # noqa: BLE001 - 取り込み失敗で画面を落とさない
            st.error(f"ファイルを読み込めませんでした: {exc}")

    if kind == "children":
        render_importer()

    frame = st.session_state.get(f"frame_{kind}", None)
    if frame is None or frame.empty:
        st.info(
            "サンプルデータを読み込むか、ファイルをアップロードするか、"
            "下のエディタに直接入力してください。"
        )
        frame = pd.DataFrame(columns=columns)
    else:
        frame = coerce_frame(kind, frame)
        st.dataframe(
            frame.head(200),
            width="stretch",
            hide_index=True,
            key=f"preview_{kind}",
        )
        st.caption(f"現在 {len(frame)} 行（プレビューは先頭 200 行）")

    edited = st.data_editor(
        frame,
        num_rows="dynamic",
        hide_index=True,
        column_config=column_config(kind),
        key=f"editor_{kind}",
        height=320,
        width="stretch",
    )
    coerced = coerce_frame(kind, edited)
    _record_edit(kind, coerced)
    st.session_state[f"frame_{kind}"] = coerced
    st.caption(f"編集中 {len(edited)} 行。行を追加・削除できます。")

    render_table_validation(kind)


def render_table_validation(kind: str) -> None:
    """その表だけの検証結果を表示する（行・列・理由を提示）。"""
    report = current_report()
    issues = report.by_table(kind)
    if not issues:
        return
    errors = [i for i in issues if i.level == ERROR]
    if errors:
        st.error(
            f"❌ {len(errors)} 件の入力を直す必要があります"
            "（下の「この内容で読み込む」は押せません）。"
        )
    else:
        st.warning(f"⚠️ 注意 {len(issues)} 件があります。")
    st.dataframe(
        report.to_dataframe(),
        hide_index=True,
        width="stretch",
        key=f"validation_{kind}",
    )


def current_report() -> ValidationReport:
    """3 表をまとめて検証した結果を取得し、session_state に保存する。"""
    settings = state.current_settings()
    report = validate_frames(
        st.session_state.get("frame_children"),
        st.session_state.get("frame_staff"),
        st.session_state.get("frame_preferences"),
        days=state.current_days(),
        day_open=settings.day_open,
        day_close=settings.day_close,
    )
    state.set(state.KEY_VALIDATION, report)
    return report


def render_importer() -> None:
    """園業務支援システム（CoDMON / キッズリー）の CSV 取り込み UI。"""
    with st.expander("🔗 園業務支援システムから取り込む", expanded=False):
        st.caption(
            "CoDMON / キッズリー などで出力した園児の登降園予定 CSV を、"
            "列名を自動で読み替えて取り込みます。"
        )
        uploaded = st.file_uploader(
            "取り込む CSV / Excel", type=["csv", "xlsx"], key="import_children"
        )
        if uploaded is None:
            st.info("CSV ファイルを選んでください。")
            return
        try:
            raw = uploaded.getvalue() if hasattr(uploaded, "getvalue") else uploaded
            source = data_loader.read_table(raw, "children")
        except Exception as exc:  # noqa: BLE001 - 取り込み失敗で画面を落とさない
            st.error(f"ファイルを読み込めませんでした: {exc}")
            return
        options = importers.profile_options()
        labels = {key: label for key, label in options}
        guessed = importers.detect_profile(source)
        chosen = st.selectbox(
            "取り込み元の形式",
            options=[key for key, _label in options],
            index=[key for key, _label in options].index(guessed),
            format_func=lambda key: labels[key],
            key="import_profile",
        )
        standard = state.current_standard()
        result = importers.convert(source, chosen, standard_time=standard.standard_time)
        st.caption(
            f"取り込み元: {uploaded.name} ／ {len(source)} 行 → 本アプリ形式 {len(result.frame)} 行"
        )
        if result.missing:
            st.warning(
                "対応する列が見つからず空欄にした列: "
                f"{', '.join(result.missing)}。取り込み後に下表で確認してください。"
            )
        for note in result.notes:
            st.caption(f"・{note}")
        st.dataframe(
            result.frame.head(20),
            hide_index=True,
            width="stretch",
            key="import_preview",
        )
        st.dataframe(
            importers.mapping_frame(result),
            hide_index=True,
            width="stretch",
            key="import_mapping",
        )
        if st.button(
            "✅ この内容で園児表に取り込む",
            key="import_apply",
            width="stretch",
        ):
            st.session_state["frame_children"] = result.frame
            st.session_state.pop("editor_children", None)
            _record_edit("children", result.frame)
            st.toast(f"園児 {len(result.frame)} 行を取り込みました", icon="✅")


def _offer_templates(kind: str, title: str) -> None:
    """テンプレート CSV を一時生成してダウンロードさせる。"""
    try:
        with TemporaryDirectory() as tmp:
            paths = data_loader.write_template_csvs(tmp)
            payload = Path(paths[kind]).read_bytes()
        st.download_button(
            f"⬇️ {title} のテンプレートをダウンロード",
            data=payload,
            file_name=f"{kind}_template.csv",
            mime="text/csv",
            key=f"template_dl_{kind}",
        )
    except Exception as exc:  # noqa: BLE001 - テンプレート生成失敗で画面を落とさない
        st.error(f"テンプレートを生成できませんでした: {exc}")


def build_load_result() -> Any:
    """3 つのエディタ表から ``LoadResult`` を作る。"""
    frames = {}
    for kind, _title, _label, _detail in TABLE_KINDS:
        frame = st.session_state.get(f"frame_{kind}", None)
        frames[kind] = None if frame is None or frame.empty else frame
    return data_loader.load_bundle(
        children_df=frames["children"],
        staff_df=frames["staff"],
        preferences_df=frames["preferences"],
    )


def apply_load_result(result: Any) -> None:
    """読み込み結果を session_state に反映する。途中で失敗したら元に戻す。"""
    with state.editing_guard(state.KEY_STAFF):
        state.set(state.KEY_LOAD_RESULT, result)
        state.set(state.KEY_CHILDREN, result.children)
        state.set(state.KEY_STAFF, result.staff)
        state.set(state.KEY_PREFERENCES, result.preferences)
        state.invalidate_pipeline()
        # ここから先の入力変更を検知するため、基準の指紋を記録する。
        state.mark_inputs_baseline()


def render_result_panel() -> None:
    """読み込み結果のサマリとエラー表。"""
    result = state.get(state.KEY_LOAD_RESULT)
    if result is None:
        st.info("まだデータを読み込んでいません。")
        return
    st.markdown("#### 📥 読み込み結果")
    st.info(result.summary())
    if result.errors:
        st.error(f"エラー {len(result.errors)} 件。該当行を確認してください。")
        st.dataframe(
            components.load_issue_dataframe(result.issues),
            hide_index=True,
            width="stretch",
            key="load_issues",
        )
    elif result.warnings:
        st.warning(f"警告 {len(result.warnings)} 件。読み込みは続いています。")
        st.dataframe(
            components.load_issue_dataframe(result.issues),
            hide_index=True,
            width="stretch",
            key="load_issues",
        )
    if not result.staff:
        st.warning("職員データが 0 名です。先に職員データを投入してください。")
    children_days = {child.day for child in result.children}
    if children_days:
        plan = state.current_days()
        covered = f"{min(children_days).isoformat()} 〜 {max(children_days).isoformat()}"
        st.caption(f"園児データがカバーしている期間: {covered}")
        outside = [d for d in plan if d not in children_days]
        if outside:
            st.warning(
                f"計画期間のうち {len(outside)} 日分の園児データがありません"
                f"（{theme.format_day(min(outside))} など）。"
                "その日の在園児は 0 名として扱われるので、必要人員も 0 になります。"
            )


def render_validation_summary(report: ValidationReport) -> None:
    """3 表ぶんの検証結果をまとめて表示する。"""
    if not report.issues:
        st.success("✅ 入力チェック: 指摘はありません。")
        return
    if report.has_errors:
        st.error(
            f"🚫 入力チェック: {report.summary()}。エラーがあるため読み込みできません。"
            "下の表の「行」「列」を直してください。"
        )
    else:
        st.warning(f"⚠️ 入力チェック: {report.summary()}（読み込みは続行できます）。")
    st.dataframe(
        report.to_dataframe(),
        hide_index=True,
        width="stretch",
        key="validation_summary",
    )


def reset_tables() -> None:
    """3 表（園児・職員・希望休）と編集履歴だけを初期化する。

    園設定・配置基準・計画期間・最適化オプションは**保持**する。
    ラベルが表す範囲と実際に消える範囲を一致させるために、
    全消去版の :func:`reset_everything` と分けてある。
    """
    for kind, _t, _l, _d in TABLE_KINDS:
        st.session_state.pop(f"frame_{kind}", None)
        st.session_state.pop(f"editor_{kind}", None)
        st.session_state.pop(f"upload_{kind}", None)
    state.reset_edit_histories()


def reset_everything() -> None:
    """セッション全体を初期化する（3 表・園設定・基準・計算結果すべて）。

    ``state.reset_all()`` を呼ぶ。ウィザードのステップも 0 に戻す。

    文字列リテラル ``"wizard_step"`` を使うのは、``wizard.py`` が
    ``tab_data`` を import しているのでここで ``wizard`` を import すると
    循環するため。キーの一致は ``tests/test_ui_wizard.py`` が固定する。
    """
    reset_tables()
    state.reset_all()
    st.session_state.pop("wizard_step", None)


def render_apply_block() -> None:
    """検証サマリ・読み込みボタン・読み込み結果パネル（両モード共通）。

    「まとめて入力」（``render``）と「ウィザード」（``wizard.render``）の
    最後の画面が同じなので、描画だけをここへ寄せる。ボタンの ``key`` は
    どちらのモードでも 1 つだけ存在することを前提にして共有している
    （同じ実行内で二重に描画されない）。
    """
    st.divider()
    report = current_report()
    render_validation_summary(report)
    left, right = st.columns(2)
    with left:
        apply_clicked = st.button(
            "✅ この内容で読み込む",
            type="primary",
            width="stretch",
            key="apply_load",
            disabled=report.has_errors,
            help=(
                "修正が必要な入力があります。上の一覧の行・列を確認してください。"
                if report.has_errors
                else "3 表を配置基準エンジンへ読み込みます。"
            ),
        )
    with right:
        if st.button("🧹 3 表だけをクリア", width="stretch", key="clear_tables"):
            reset_tables()
            st.rerun()
        if st.button(
            "🔄 入力と計算結果をすべて初期化",
            width="stretch",
            key="clear_all",
            help="3 表・園設定・配置基準・計画期間・計算結果をすべて消します。取り消せません。",
        ):
            st.session_state["_confirm_reset_all"] = True
            st.rerun()
    if st.session_state.get("_confirm_reset_all"):
        st.warning("3 表・園設定・配置基準・計画期間・計算結果をすべて消します。")
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            if st.button("⚠️ 本当にすべて初期化する", key="clear_all_confirm"):
                st.session_state["_confirm_reset_all"] = False
                reset_everything()
                st.rerun()
        with cancel_col:
            if st.button("↩︎ やめる", key="clear_all_cancel"):
                st.session_state["_confirm_reset_all"] = False
                st.rerun()
    if apply_clicked:
        try:
            with st.spinner("設定を読み込んでいます…"):
                apply_load_result(build_load_result())
            st.success(
            f"読み込みました。{theme.tab_ref(2)}（シフト作成）で自動作成できます。"
        )
        except Exception as exc:  # noqa: BLE001 - 読み込み失敗で画面を落とさない
            st.error(f"読み込みに失敗しました: {exc}")

    render_result_panel()


def render_sample_tip(seed: int) -> None:
    """サンプルデータの規模をツールチップで示す。"""
    theme.tip(
        "ℹ️ サンプルデータの規模",
        f"<p>園児 {sample_data.TOTAL_CHILDREN} 名 / 職員 {sample_data.TOTAL_STAFF} 名。</p>"
        f"<p>乱数シード <b>{int(seed)}</b> のあいだは必ず同じ内容が入ります。</p>",
    )


def render() -> None:
    """タブ1 の本体（まとめて入力する画面）。"""
    theme.step_indicator(0)
    theme.heading(0)
    settings = state.current_settings()
    st.caption(
        f"現在の園設定: {settings.facility_name} ／ "
        f"{settings.day_open.strftime('%H:%M')}-{settings.day_close.strftime('%H:%M')} ／ "
        f"粒度 {settings.granularity_min} 分"
    )
    theme.tip(
        "❓ このタブで何をするの？",
        "<p>園児の登降園予定・職員・希望休の 3 つを投入します。</p>"
        "<p>ファイルがない場合は、まず<b>サンプルデータを読み込む</b>ボタンから始めて、"
        "実際の運用に合わせて少しずつ差し替えてください。</p>",
    )
    seed = st.number_input(
        "サンプルデータの共通シード",
        min_value=0,
        max_value=9999,
        value=42,
        step=1,
        key="sample_seed",
        help="変えると、各表の「乱数シード」も同じ値に戻ります。同じシードなら同じデータが入ります。",
    )
    sync_seed_inputs(int(seed))
    days = state.current_days()
    if not days:
        st.warning(theme.where_is_period())
        theme.stuck_hint(0, "計画期間が設定されていません。")
        return

    for kind, title, label, detail in TABLE_KINDS:
        with st.expander(title, expanded=kind == "children"):
            _preview_table(kind, title, label, detail)
            _render_history_controls(kind)

    render_apply_block()
    theme.caveat_box()
    theme.next_step_hint(0)
    render_sample_tip(int(seed))
