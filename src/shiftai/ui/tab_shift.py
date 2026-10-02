"""タブ4「シフト表・微調整」: 日別のシフトグリッド表示と手動編集・再最適化。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import streamlit as st

from shiftai import gap_analysis, live_validation
from shiftai.domain import CellState, SolveResult
from shiftai.ui import components, gantt, state, theme

LOCK = components.LOCK


def _current_day() -> date | None:
    """selectbox で選ばれている日を返す。"""
    result = state.get(state.KEY_SOLVE_RESULT)
    options = components.day_options(result)
    if not options:
        return None
    return st.selectbox(
        "日付",
        options=options,
        format_func=theme.format_day,
        key="active_day",
    )


def _render_grid(result: SolveResult, day: date, slots: Any, staff: list[Any]) -> None:
    """シフトグリッドと職員別サマリ。"""
    fixed = state.normalize_fixed(state.get(state.KEY_FIXED_ASSIGNMENTS))
    shift_day = result.day(day)
    st.markdown(f"#### 🗓 {theme.format_day(day)} のシフト")
    components.cell_legend()
    view = st.radio(
        "表示形式",
        options=["マトリクス", "ガントチャート"],
        horizontal=True,
        key="shift_grid_view",
        help="「ガントチャート」は職員ごとに勤務・休憩の横棒を時間軸上に並べます。"
        "時間帯ごとの有無を1セルずつ追うときは「マトリクス」が向いています。",
    )
    left, right = st.columns([3, 1])
    with left:
        if view == "ガントチャート":
            gantt.render(shift_day, slots, staff, day=day, fixed=fixed)
        else:
            frame = components.shift_grid_frame(shift_day, slots, staff, day=day, fixed=fixed)
            if frame.empty:
                st.info("この日のシフトがありません。")
            else:
                st.dataframe(
                    components.style_shift_grid(frame),
                    width="stretch",
                    height=520,
                    key=f"shift_grid_{day.isoformat()}",
                )
    with right:
        summary = components.staff_day_summary_frame(shift_day, slots, staff)
        st.markdown("##### 職員別サマリ")
        st.dataframe(
            summary,
            hide_index=True,
            width="stretch",
            height=520,
            key=f"shift_summary_{day.isoformat()}",
            column_config={
                "職員ID": st.column_config.TextColumn("職員ID", width="small"),
                "勤務分数": st.column_config.NumberColumn("勤務分数", format="%d"),
                "休憩分数": st.column_config.NumberColumn("休憩分数", format="%d"),
                "実働時間": st.column_config.NumberColumn("実働時間", format="%.2f"),
            },
        )


def _apply_edits(
    edited: pd.DataFrame,
    baseline: pd.DataFrame,
    staff: list[Any],
    slots: Any,
    day: date,
) -> tuple[dict[tuple[str, date, str], CellState], dict[tuple[int, str], str]]:
    """編集差分から ``(確定マッピング, 差分)`` を作る。"""
    changes = components.diff_edits(edited, baseline)
    if not changes:
        return {}, {}
    labels = [components.row_label(m.staff_id, m.name) for m in staff]
    fixed = state.normalize_fixed(state.get(state.KEY_FIXED_ASSIGNMENTS))
    for (row, column), value in changes.items():
        if not (0 <= row < len(labels)):
            continue
        staff_id = labels[row].split(" ", 1)[0]
        try:
            cell = CellState(value)
        except ValueError:
            continue
        fixed[(staff_id, day, column)] = cell
    return fixed, changes


def _render_editor(result: SolveResult, day: date, slots: Any, staff: list[Any]) -> None:
    """``st.data_editor`` による手動微調整。"""
    shift_day = result.day(day)
    baseline = components.editable_grid_frame(shift_day, slots, staff)
    if baseline.empty:
        return
    st.markdown("##### ✏️ 微調整（セルの状態を変更できます）")
    st.caption(
        "「勤務」「休憩」「オフ」のいずれかを選びます。"
        "変更はすぐには確定されません。下部の「変更を確定して再最適化する」を押すと "
        f"{LOCK} 印とともに固定され、再最適化でもそのセルは動きません。"
    )
    column_config = components.editable_column_config(slots)
    edited = st.data_editor(
        baseline,
        key="shift_editor",
        hide_index=True,
        num_rows="fixed",
        disabled=["職員"],
        column_config=column_config,
        width="stretch",
        height=420,
    )
    fixed, changes = _apply_edits(edited, baseline, staff, slots, day)
    if not changes:
        st.caption("変更はありません。")
        _render_live_check(baseline, day, slots, staff, key="base")
        return

    labels = [components.row_label(m.staff_id, m.name) for m in staff]
    live_report = _render_live_check(edited, day, slots, staff, key="edited")
    st.warning(f"未確定の変更が {len(changes)} セルあります。")
    st.dataframe(
        pd.DataFrame.from_records(
            [
                {
                    "職員": labels[row] if 0 <= row < len(labels) else f"行{row}",
                    "時間帯": column,
                    "変更後": value,
                }
                for (row, column), value in sorted(changes.items())
            ]
        ),
        hide_index=True,
        width="stretch",
        key=f"pending_edits_{day.isoformat()}",
    )

    left, right = st.columns(2)
    has_errors = bool(live_report is not None and live_report.has_errors)
    with left:
        if st.button(
            f"✅ 変更を確定して再最適化する（{len(changes)} セル）",
            type="primary",
            width="stretch",
            key=f"apply_edits_{day.isoformat()}",
            # 配置基準・契約に抵触する変更は確定させない。
            # 修正前: ``disabled`` が無く、重大違反（人員不足・希望休出勤など）を
            # 含む変更でも確定でき、その結果 必要人員が法定基準を満たさない
            # 勤務表として公開されていた。
            disabled=has_errors,
            help=(
                "配置基準または契約に抵触する変更があります。"
                "上の「検証つきのビュー」で赤いセルを確認して修正してください。"
                if has_errors
                else None
            ),
        ):
            _commit_edits(fixed, len(changes))
    with right:
        if st.button(
            "↩️ 変更を破棄",
            width="stretch",
            key=f"discard_edits_{day.isoformat()}",
        ):
            st.session_state.pop("shift_editor", None)
            st.rerun()


def _render_live_check(
    frame: pd.DataFrame,
    day: date,
    slots: Any,
    staff: list[Any],
    *,
    key: str,
) -> None:
    """編集途中のグリッドを即時に検証して、セル色の付いた表と指摘一覧を出す。

    ``st.data_editor`` は ``Styler`` を渡せないため、ここでは読み取り専用の
    色付きビューを隣に出し、「確定前」に問題が見えるようにする。
    """
    table = state.get(state.KEY_REQUIREMENTS)
    if table is None:
        return
    grid = components.editable_indexed_frame(frame, slots, staff)
    report = live_validation.validate_day(
        day,
        table,
        staff,
        slots,
        grid,
        preferences=state.get(state.KEY_PREFERENCES) or {},
        settings=state.current_settings(),
        standard=state.current_standard(),
        staff_name_map=state.staff_name_map(),
    )
    labels = {components.row_label(m.staff_id, m.name): m.staff_id for m in staff}
    if report.issues:
        with st.expander(
            f"🔍 基準の即時チェック（{report.summary()}）", expanded=report.has_errors
        ):
            if report.has_errors:
                st.error(
                    "この変更のまま確定すると配置基準・契約に抵触します。"
                    "赤い枠のセルを確認してください。"
                )
            else:
                st.warning("運用上の注意があります（確定はできます）。")
            st.dataframe(
                live_validation.to_dataframe(report),
                hide_index=True,
                width="stretch",
                key=f"live_issues_{key}_{day.isoformat()}",
            )
    st.markdown("##### 🔎 検証つきのビュー（確定前）")
    st.dataframe(
        components.style_live_grid(grid, report, row_labels=labels),
        width="stretch",
        height=420,
        key=f"live_grid_{key}_{day.isoformat()}",
    )
    st.caption("赤枠＝配置基準・契約に抵触、橙枠＝運用上の注意、下線＝時間帯全体で人数が不足。")
    # 呼び出し側（確定ボタン）が ``disabled=`` に使うため、
    # 検証レポートを返す。返さないと重大違反のある変更でも確定できてしまう。
    return report


def _commit_edits(fixed: dict[tuple[str, date, str], CellState], count: int) -> None:
    """確定したセルを保存し、そのまま再最適化する。"""
    from shiftai.ui import tab_solve

    state.set(state.KEY_FIXED_ASSIGNMENTS, fixed)
    st.session_state.pop("shift_editor", None)
    try:
        with st.status(f"{count} セルを確定し、再最適化しています…", expanded=True) as status:
            tab_solve.run_solve()
            status.update(label="再最適化が完了しました", state="complete")
        st.rerun()
    except Exception as exc:  # noqa: BLE001 - 再最適化の失敗で画面を落とさない
        st.error(f"再最適化に失敗しました（確定は保持されています）: {exc}")


def _render_fixed_list() -> None:
    """固定済みセルの一覧と全解除。"""
    fixed = state.normalize_fixed(state.get(state.KEY_FIXED_ASSIGNMENTS))
    with st.expander(f"🔒 手動で確定したセル（{len(fixed)} 件）", expanded=False):
        if not fixed:
            st.caption("確定されたセルはありません。")
            return
        names = state.staff_name_map()
        records = [
            {
                "職員ID": staff_id,
                "氏名": names.get(staff_id, ""),
                "日付": theme.format_day(fixed_day),
                "時間帯": label,
                "状態": CellState(cell).value,
            }
            for (staff_id, fixed_day, label), cell in sorted(
                fixed.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2])
            )
        ]
        st.dataframe(records, hide_index=True, width="stretch", key="fixed_list")
        if st.button("🔓 すべての確定を解除する", key="clear_fixed", width="stretch"):
            state.reset(state.KEY_FIXED_ASSIGNMENTS)
            st.session_state.pop("shift_editor", None)
            st.rerun()


def _render_weekday_pivot(result: SolveResult, slots: Any) -> None:
    """職員×曜日の勤務分数ピボット表。"""
    st.markdown("#### 📅 曜日別サマリー（勤務分数）")
    frame = components.weekday_pivot_frame(result, slots)
    if frame.empty:
        st.info("集計できるデータがありません。")
        return
    st.dataframe(frame, width="stretch", key="weekday_pivot", height=400)
    st.caption("各曜日の合計を、本アプリが使う週 44 時間の内部目安と照らし合わせてください。")


def _render_coverage_matrix(result: SolveResult) -> None:
    """基準充足マトリクス。"""
    table = state.get(state.KEY_REQUIREMENTS)
    staff = state.get(state.KEY_STAFF) or []
    if table is None:
        return
    with st.expander("🧮 基準充足マトリクス（配置数／必要数）", expanded=False):
        frame = gap_analysis.coverage_matrix(table, result, staff)
        if frame.empty:
            st.info("表示できるデータがありません。")
            return
        styled = frame.style.map(
            lambda v: (
                ""
                if "/" not in str(v)
                else (
                    "background-color: #E5393533; color: #8e0000; font-weight:600;"
                    if int(str(v).split("/")[0]) < int(str(v).split("/")[1])
                    else "background-color: #2E7D321a; color: #1b5e20;"
                )
            )
        )
        st.dataframe(styled, width="stretch", key="coverage_matrix")
        st.caption("赤＝配置数が必要数に届いていない時間帯（法令・基準の未達）。")


def render() -> None:
    """タブ4 の本体。"""
    theme.step_indicator(3)
    st.markdown("### 4. シフト表・微調整")
    result = state.get(state.KEY_SOLVE_RESULT)
    if result is None or not result.shift_days:
        theme.empty_state("まずタブ3「シフト作成」でシフトを作成してください。")
        return
    staff = state.get(state.KEY_STAFF) or []
    if not staff:
        st.warning("職員データが未投入です。")
        return
    slots = state.current_slots()
    if not slots:
        st.error("時間帯を生成できません。サイドバーの開所・閉所時刻を確認してください。")
        return

    day = _current_day()
    if day is None:
        st.warning("表示できる日がありません。")
        return

    _render_grid(result, day, slots, staff)
    st.divider()
    _render_editor(result, day, slots, staff)
    st.divider()
    _render_fixed_list()
    st.divider()
    _render_weekday_pivot(result, slots)
    st.divider()
    _render_coverage_matrix(result)
    st.divider()
    components.render_fairness_panel(
        result,
        staff,
        slots,
        standard=state.current_standard(),
        patterns=state.current_patterns(),
    )
    theme.caveat_box()
    theme.next_step_hint(3)
