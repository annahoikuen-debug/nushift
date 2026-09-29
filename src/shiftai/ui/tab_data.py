"""タブ1「データ投入」: 園児（登降園予定）・職員・希望休の読み込みと直接編集。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pandas as pd
import streamlit as st

from shiftai import data_loader, sample_data
from shiftai.domain import AgeClass, EmploymentType, Role
from shiftai.ui import components, state, theme

TABLE_KINDS: tuple[tuple[str, str, str], ...] = (
    (
        "children",
        "🧒 園児（登降園予定）",
        "園児が 1 プラン 1 行。短時間保育・早朝保育・延長保育のフラグで必要人員が変わります。"
        "列: 園児ID / 氏名 / 年齢 / 登園日 / 登園時刻 / 降園時刻 / 短時間保育 / 欠席 / 早朝保育 / 延長保育",
    ),
    (
        "staff",
        "👩‍🏫 職員",
        "契約時間がそのまま「供給できる人時」になります。週契約時間を増やさないと基準を満たせません。"
        "列: 職員ID / 氏名 / 資格（主・副）/ 雇用形態 / 週・1日契約時間 / 最早始業・最遅終業",
    ),
    (
        "preferences",
        "🌴 希望休",
        "「出勤不可」「希望休」はハード制約（シフトを割り当てません）。それ以外はソフト制約です。"
        "列: 職員ID / 種別（出勤不可・希望休・休み希望・出勤希望）/ 日付 / 開始 / 終了 / 理由",
    ),
)

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


def _column_config(kind: str) -> dict[str, Any]:
    """``st.data_editor`` 用の日本語 ``column_config``。"""
    if kind == "children":
        return {
            "園児ID": st.column_config.TextColumn("園児ID", required=True, width="small"),
            "氏名": st.column_config.TextColumn("氏名", required=True),
            "年齢": st.column_config.SelectboxColumn(
                "年齢", options=[str(a.years) for a in AgeClass], required=True
            ),
            "登園日": st.column_config.DateColumn("登園日", format="YYYY/MM/DD", required=True),
            "登園時刻": st.column_config.TimeColumn("登園時刻", format="HH:MM", step=900),
            "降園時刻": st.column_config.TimeColumn("降園時刻", format="HH:MM", step=900),
            "短時間保育": st.column_config.SelectboxColumn("短時間保育", options=list(YES_NO)),
            "欠席": st.column_config.SelectboxColumn("欠席", options=list(YES_NO)),
            "欠席理由": st.column_config.TextColumn("欠席理由"),
            "早朝保育": st.column_config.SelectboxColumn("早朝保育", options=list(YES_NO)),
            "延長保育": st.column_config.SelectboxColumn("延長保育", options=list(YES_NO)),
            "備考": st.column_config.TextColumn("備考"),
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
            "週契約時間": st.column_config.NumberColumn("週契約時間", min_value=0.0, max_value=80.0, step=0.5, format="%.1f"),
            "1日契約時間": st.column_config.NumberColumn("1日契約時間", min_value=0.5, max_value=14.0, step=0.5, format="%.1f"),
            "月間最小時間": st.column_config.NumberColumn("月間最小時間", min_value=0.0, max_value=400.0, step=1.0),
            "月間最大時間": st.column_config.NumberColumn("月間最大時間", min_value=0.0, max_value=400.0, step=1.0),
            "週最大出勤日数": st.column_config.NumberColumn("週最大出勤日数", min_value=0, max_value=7, step=1, format="%d"),
            "最大連続勤務日数": st.column_config.NumberColumn("最大連続勤務日数", min_value=1, max_value=7, step=1, format="%d"),
            "最早始業": st.column_config.TimeColumn("最早始業", format="HH:MM", step=900),
            "最遅終業": st.column_config.TimeColumn("最遅終業", format="HH:MM", step=900),
            "能力タグ": st.column_config.TextColumn("能力タグ", help="| で区切ります（例: 乳幼児研修修了|ピアノ指導可）"),
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
        "開始": st.column_config.TimeColumn("開始", format="HH:MM", step=900),
        "終了": st.column_config.TimeColumn("終了", format="HH:MM", step=900),
        "理由": st.column_config.TextColumn("理由"),
    }


def _seed_for_table() -> int:
    return int(st.session_state.get("sample_seed", 42))


def _sync_seed_inputs(master: int) -> None:
    """共通シードを変えたら、各表のシード入力を既定値に戻す。"""
    previous = st.session_state.get("sample_seed_applied", master)
    if previous == master:
        return
    for kind, _title, _desc in TABLE_KINDS:
        st.session_state.pop(f"sample_seed_{kind}", None)
    st.session_state["sample_seed_applied"] = master


def _load_sample(kind: str, days: Sequence[date], seed: int) -> pd.DataFrame:
    """``sample_data`` から該当テーブルの DataFrame を作る。"""
    children, staff, preferences = sample_data.make_dataset(days, seed=seed)
    frames = sample_data.sample_dataframes(
        children=children, staff=staff, preferences=preferences
    )
    return frames[kind]


def _read_uploads(kind: str, files: Sequence[Any]) -> list[pd.DataFrame]:
    """アップロードされたファイルを DataFrame として読む。"""
    frames: list[pd.DataFrame] = []
    for uploaded in files:
        raw = uploaded.getvalue() if hasattr(uploaded, "getvalue") else uploaded
        frames.append(data_loader.read_table(raw, kind))
    return frames


def _preview_table(kind: str, title: str, description: str) -> None:
    columns = data_loader.TABLE_COLUMNS[kind]
    days = state.current_days()
    st.markdown(f"#### {title}")
    st.caption(description)
    st.caption(f"想定される列: {', '.join(columns)}")

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
            frame = _load_sample(
                kind, days, int(st.session_state.get(f"sample_seed_{kind}", 42))
            )
            st.session_state[f"frame_{kind}"] = frame
            st.session_state[f"seed_{kind}"] = int(
                st.session_state.get(f"sample_seed_{kind}", 42)
            )
            st.toast(f"{title} のサンプルを {len(frame)} 行読み込みました", icon="✅")

    uploads = st.file_uploader(
        f"{title} のファイル（CSV / Excel）",
        type=["csv", "xlsx"],
        accept_multiple_files=True,
        key=f"upload_{kind}",
    )
    if uploads:
        try:
            frames = _read_uploads(kind, uploads)
            merged = pd.concat(frames, ignore_index=True)
            st.session_state[f"frame_{kind}"] = merged
            st.success(f"{len(merged)} 行を読み込みました。")
        except Exception as exc:  # noqa: BLE001 - 取り込み失敗で画面を落とさない
            st.error(f"ファイルを読み込めませんでした: {exc}")

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
        column_config=_column_config(kind),
        key=f"editor_{kind}",
        height=320,
        width="stretch",
    )
    st.session_state[f"frame_{kind}"] = coerce_frame(kind, edited)
    st.caption(f"編集中 {len(edited)} 行。行を追加・削除できます。")


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


def _build_load_result() -> Any:
    """3 つのエディタ表から ``LoadResult`` を作る。"""
    frames = {}
    for kind, _title, _desc in TABLE_KINDS:
        frame = st.session_state.get(f"frame_{kind}", None)
        frames[kind] = None if frame is None or frame.empty else frame
    return data_loader.load_bundle(
        children_df=frames["children"],
        staff_df=frames["staff"],
        preferences_df=frames["preferences"],
    )


def _apply_load_result(result: Any) -> None:
    """読み込み結果を session_state に反映する。途中で失敗したら元に戻す。"""
    with state.editing_guard(state.KEY_STAFF):
        state.set(state.KEY_LOAD_RESULT, result)
        state.set(state.KEY_CHILDREN, result.children)
        state.set(state.KEY_STAFF, result.staff)
        state.set(state.KEY_PREFERENCES, result.preferences)
        state.invalidate_pipeline()


def _render_result_panel() -> None:
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


def render() -> None:
    """タブ1 の本体。"""
    st.markdown("### 1. データ投入")
    st.caption(
        "園児の登降園予定・職員・希望休の 3 つを投入します。"
        "ファイルがない場合はサンプルデータから始めて、実際の運用に合わせて少しずつ差し替えてください。"
    )
    settings = state.current_settings()
    st.caption(
        f"現在の園設定: {settings.facility_name} ／ "
        f"{settings.day_open.strftime('%H:%M')}-{settings.day_close.strftime('%H:%M')} ／ "
        f"粒度 {settings.granularity_min} 分"
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
    _sync_seed_inputs(int(seed))
    days = state.current_days()
    if not days:
        st.warning("サイドバーで計画期間を設定してください。")
        return

    for kind, title, description in TABLE_KINDS:
        with st.expander(title, expanded=kind == "children"):
            _preview_table(kind, title, description)

    st.divider()
    left, right = st.columns(2)
    with left:
        apply_clicked = st.button(
            "✅ この内容で読み込む",
            type="primary",
            width="stretch",
            key="apply_load",
        )
    with right:
        if st.button("🗑 すべてクリア", width="stretch", key="clear_all"):
            for kind, _t, _d in TABLE_KINDS:
                st.session_state.pop(f"frame_{kind}", None)
                st.session_state.pop(f"editor_{kind}", None)
                st.session_state.pop(f"upload_{kind}", None)
            state.reset_all()
            st.rerun()
    if apply_clicked:
        try:
            with st.spinner("設定を読み込んでいます…"):
                _apply_load_result(_build_load_result())
            st.success("読み込みました。タブ2「必要人員」で基準を計算できます。")
        except Exception as exc:  # noqa: BLE001 - 読み込み失敗で画面を落とさない
            st.error(f"読み込みに失敗しました: {exc}")

    _render_result_panel()
    theme.caveat_box()
    st.caption(
        f"サンプルデータの規模: 園児 {sample_data.TOTAL_CHILDREN} 名 / "
        f"職員 {sample_data.TOTAL_STAFF} 名（seed={int(seed)} で毎回同じ内容になります）"
    )
