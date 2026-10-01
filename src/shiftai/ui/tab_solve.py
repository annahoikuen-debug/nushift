"""タブ3「シフト自動作成」: PuLP（CBC）による最適化の実行と結果表示。"""

from __future__ import annotations

from typing import Any

import streamlit as st

from shiftai import gap_analysis, solver, standards
from shiftai.domain import SolveResult
from shiftai.ui import components, state, theme

PROGRESS_STEPS: tuple[tuple[float, str], ...] = (
    (0.05, "設定を検証しています…"),
    (0.15, "配置基準エンジンで必要人員を再計算しています…"),
    (0.25, "最適化モデルを構築しています（変数・制約の生成）…"),
    (0.35, "PuLP（CBC）で最適化しています。この処理は数十秒かかることがあります…"),
    (0.80, "解をデコードしてシフト表にしています…"),
    (0.90, "過不足と法令違反を検証しています…"),
    (1.00, "完了しました。"),
)


def build_requirements() -> Any:
    """タブ2 と同じ条件で ``RequirementTable`` を作り直す。"""
    from shiftai.ui import tab_requirements

    return tab_requirements.build_requirements()


def run_solve(*, force: bool = False) -> SolveResult | None:
    """最適化を実行して結果を session_state に保存する。

    ``force`` が True のときは再計算を強制する。例外は呼び出し側で表示する。
    """
    settings = state.current_settings()
    standard = state.current_standard()
    children = state.get(state.KEY_CHILDREN) or []
    staff = state.get(state.KEY_STAFF) or []
    preferences = state.get(state.KEY_PREFERENCES) or {}
    weights = state.sync_weights()
    fixed = state.normalize_fixed(state.get(state.KEY_FIXED_ASSIGNMENTS))
    if not children:
        raise ValueError("園児データが未投入です。")
    if not staff:
        raise ValueError("職員データが未投入です。")

    limit = int(state.get(state.KEY_TIME_LIMIT_SEC))
    progress = st.progress(0.0, text=PROGRESS_STEPS[0][1])
    try:
        progress.progress(PROGRESS_STEPS[0][0], text=PROGRESS_STEPS[0][1])
        table = build_requirements()
        state.set(state.KEY_REQUIREMENTS, table)
        progress.progress(PROGRESS_STEPS[1][0], text=PROGRESS_STEPS[1][1])
        progress.progress(PROGRESS_STEPS[2][0], text=PROGRESS_STEPS[2][1])
        with st.spinner(PROGRESS_STEPS[3][1]):
            result = solver.solve_shift(
                children,
                staff,
                table,
                preferences,
                weights,
                fixed_assignments=fixed,
                settings=settings,
                time_limit_sec=limit,
                msg=False,
                standard=standard,
            )
        progress.progress(PROGRESS_STEPS[4][0], text=PROGRESS_STEPS[4][1])
        report = gap_analysis.analyze_gap(table, result, staff, standard=standard)
        progress.progress(PROGRESS_STEPS[5][0], text=PROGRESS_STEPS[5][1])
        violations = gap_analysis.check_violations(
            table, result, staff, preferences, standard=standard, settings=settings
        )
        progress.progress(PROGRESS_STEPS[6][0], text=PROGRESS_STEPS[6][1])
    finally:
        progress.empty()

    state.set(state.KEY_SOLVE_RESULT, result)
    state.set(state.KEY_GAP_REPORT, report)
    state.set(state.KEY_VIOLATIONS, violations)
    st.session_state.pop("shift_editor", None)
    st.session_state.pop("gap_editor", None)
    return result


def _ratio_text(ratio: float) -> str:
    """必要／供給比を表示用文字列にする（``inf`` は「∞」表記）。"""
    if ratio == float("inf"):
        return "∞"
    return f"{ratio:.2f}"


def _render_precheck(table: Any) -> None:
    """実行前の前提確認（必要人時と供給可能人時の比）。"""
    required = standards.total_required_hours(table)
    supply = state.contract_hours()
    ratio = state.supply_demand_ratio(required)
    shown = _ratio_text(ratio)
    with st.expander("✅ 最適化の前提を確認する", expanded=True):
        components.metric_row(
            [
                ("必要人時", f"{required:,.1f} h", "配置基準が必要とする総人時"),
                (
                    "供給可能人時",
                    f"{supply:,.1f} h",
                    f"{len(state.get(state.KEY_STAFF) or [])} 名 × 契約時間",
                ),
                (
                    "必要／供給 比",
                    ("!" if ratio > 1.0 else "~") + shown,
                    "1.00 超で構造的な不足",
                ),
                (
                    "ピーク必要人員",
                    f"{standards.peak_requirement(table)} 人",
                    "1 日で同時に必要な最大人数",
                ),
                (
                    "実行時間上限",
                    f"{int(state.get(state.KEY_TIME_LIMIT_SEC))} 秒",
                    "PuLP（CBC）の時間制限",
                ),
            ]
        )
        if required <= 0:
            st.info("必要人時が 0 h です。園児の登降園予定が登録されていません。")
        elif supply <= 0:
            st.error(
                f"供給可能人時が {supply:,.1f} h です。必要人時 {required:,.1f} h を"
                "満たす人員が 1 人も配置できません（比 = ∞）。"
                "職員の契約時間帯や休園日の設定を確認してください。"
            )
        elif ratio > 1.0:
            st.error(
                f"必要人時 {required:,.1f} h ＞ 供給可能人時 {supply:,.1f} h（比 {shown}）。"
                "**職員数・契約時間・園児数のいずれか进行调整しないと"
                "配置基準を満たすシフトは作成できません。**"
            )
        else:
            st.success(
                f"必要／供給比は {shown} です。時間としては充足可能です。"
                "ただし出勤日数・希望休の制約により時間帯単位で不足が出る場合があります。"
            )
        st.caption(
            f"セル変数 {len(state.get(state.KEY_STAFF) or []) * len(table.slots) * len(table.all_days()):,} 個"
            f" 程度。人数×日数×時間帯に比例するため、"
            "職員数や日数を増やすと最適化時間が大きく伸びます。"        )


def _render_kpis(result: SolveResult) -> None:
    """結果のKPIカード。"""
    report = state.get(state.KEY_GAP_REPORT)
    staff = state.get(state.KEY_STAFF) or []
    settings = state.current_settings()
    summary = gap_analysis.summarize(result, report, staff, settings) if report else {}
    cost = gap_analysis.compute_cost(result, staff, settings)
    coverage = float(report.coverage_ratio) if report else 0.0
    components.metric_row(
        [
            (
                "配置基準適合率",
                ("~%.1f%%" if coverage >= 0.999 else "!%.1f%%") % (coverage * 100.0),
                f"必要 {summary.get('必要人員時間', 0.0):,.1f} h / "
                f"配置 {summary.get('配置人員時間', 0.0):,.1f} h",
            ),
            (
                "配置不足時間帯数",
                ("!%d" if (report and report.total_shortfall_slots) else "%d")
                % (report.total_shortfall_slots if report else 0),
                f"不足 {summary.get('不足時間', 0.0):,.1f} h",
            ),
            (
                "法令違反（BLOCKER）",
                ("!%d 件" if result.blockers() else "%d 件") % len(result.blockers()),
                f"要調整 {summary.get('要調整件数', 0.0):.0f} 件",
            ),
            (
                "人件費（実働基準）",
                f"{cost:,.0f} 円",
                f"実働 × 1時間 {settings.labor_cost_per_hour:,.0f} 円 × 雇用形態係数",
            ),
            (
                "対象期間の総人時",
                f"{summary.get('総勤務時間', 0.0):,.1f} h",
                f"平均 {summary.get('平均勤務時間', 0.0):.1f} h／人",
            ),
        ]
    )


def _render_messages(result: SolveResult) -> None:
    """``SolveResult.messages`` を全文表示する。"""
    if not result.messages:
        return
    with st.expander("📝 ソルバからのメッセージ", expanded=False):
        for message in result.messages:
            st.caption(f"- {message}")


def _render_gap_table() -> None:
    """過不足テーブル。"""
    report = state.get(state.KEY_GAP_REPORT)
    if report is None:
        return
    st.markdown("#### ⚖️ 過不足テーブル")
    frame = components.gap_table_frame(report)
    if frame.empty:
        st.info("比較する時間帯がありません。")
        return
    components.metric_row(
        [
            ("不足時間帯", f"{report.total_shortfall_slots} 件", "必要人員に届かなかった時間帯"),
            ("不足人時", f"{report.total_shortfall_hours:,.2f} h", "在勤中に足りない総時間"),
            (
                "保育士不足",
                f"{report.total_qualified_shortfall_hours:,.2f} h",
                "保育士数が基準を下回った時間",
            ),
            ("過剰人時", f"{report.total_overstaff_hours:,.2f} h", "基準より余分に配置した時間"),
            (
                "最大不足日",
                theme.format_day(report.worst_day) if report.worst_day else "—",
                "最も不足した日",
            ),
        ]
    )
    st.dataframe(
        components.style_gap_table(frame),
        hide_index=True,
        width="stretch",
        height=360,
        key="gap_table",
    )
    st.caption("不足（赤）／過剰（青）を色で示しています。行数が多い場合はタブ2 の過不足ヒートマップも参照してください。")
    with st.expander("日別サマリー", expanded=False):
        st.dataframe(
            report.daily_dataframe(),
            hide_index=True,
            width="stretch",
            key="gap_daily",
        )


def _render_staffing_curve(result: SolveResult) -> None:
    """必要人員と配置人員の重ね書き（人員配置曲線）。"""
    st.markdown("#### 📈 人員配置曲線（必要人員 vs 配置人員）")
    table = state.get(state.KEY_REQUIREMENTS)
    staff = state.get(state.KEY_STAFF) or []
    days = components.days_of(result)
    if table is None or not days:
        st.info("比較できるデータがありません。")
        return
    day = st.selectbox(
        "表示する日",
        options=days,
        format_func=theme.format_day,
        key="curve_day",
    )
    frame = components.staffing_curve_frame(table, result, day, staff)
    if frame.empty:
        st.info("この日のデータがありません。")
        return
    chart = frame.set_index("時間帯")[["必要人員", "配置人員", "必要保育士数"]]
    st.line_chart(chart, height=320, width="stretch")
    st.caption("「必要人員」と「配置人員」が重なっていれば、基準を満たしています。")
    with st.expander("数値表（背景色で過不足を表示）", expanded=False):
        styled = frame.set_index("時間帯").style.map(
            lambda v: (
                "background-color: #E5393533; color: #8e0000; font-weight:600;"
                if str(v) and float(v) < 0
                else ""
            ),
            subset=["過不足"],
        )
        st.dataframe(styled, width="stretch", key="curve_table")
    with st.expander("職員別勤務時間", expanded=True):
        hours = components.staff_hours_frame(result, staff)
        if hours.empty:
            st.info("勤務時間データがありません。")
        else:
            st.bar_chart(
                hours.set_index("職員ID")[["勤務時間"]],
                height=300,
                width="stretch",
            )
            st.dataframe(
                hours,
                hide_index=True,
                width="stretch",
                key="staff_hours_table",
                column_config={
                    "職員ID": st.column_config.TextColumn("職員ID", width="small"),
                    "勤務時間": st.column_config.NumberColumn("勤務時間", format="%.2f"),
                    "出勤日数": st.column_config.NumberColumn("出勤日数", format="%d"),
                },
            )
            st.caption(
                "週 44 時間は本アプリが使う内部の目安です（法定の枠組みは"
                "月45時間・年360時間。旧来の週44時間は2019年の改正で"
                "法定の上限ではなくなっています）。個人別に 40 時間前後に"
                "収まっているかは必ず確認してください。"
            )


def render() -> None:
    """タブ3 の本体。"""
    st.markdown("### 3. シフト自動作成")
    if not state.data_ready():
        theme.empty_state()
        return
    table = state.get(state.KEY_REQUIREMENTS)
    if table is None:
        st.warning("先にタブ2「必要人員」で「必要人員を再計算」を押してください。")
        return

    _render_precheck(table)

    if st.button(
        "🚀 シフトを自動作成する",
        type="primary",
        width="stretch",
        key="run_solve",
    ):
        try:
            with st.status("最適化を実行中…", expanded=True) as status:
                result = run_solve(force=True)
                if result is not None:
                    st.write(
                        f"ステータス: **{getattr(result.status, 'value', result.status)}**"
                    )
                status.update(label="最適化が完了しました", state="complete")
            st.success("シフトを作成しました。")
        except Exception as exc:  # noqa: BLE001 - 最適化の失敗で画面を落とさない
            st.error(f"シフトの作成に失敗しました: {exc}")

    result = state.get(state.KEY_SOLVE_RESULT)
    if result is None:
        st.info("「シフトを自動作成する」を押すと、ここに結果が表示されます。")
        return

    st.divider()
    components.status_banner(result)
    _render_kpis(result)
    _render_messages(result)

    fixed = state.normalize_fixed(state.get(state.KEY_FIXED_ASSIGNMENTS))
    if fixed:
        st.info(
            f"手動で確定したセルが {len(fixed)} 件あります。再最適化してもこのセルは動きません。"
        )

    st.divider()
    _render_gap_table()
    st.divider()
    st.markdown("#### ⚠️ 違反一覧")
    violations = state.get(state.KEY_VIOLATIONS) or result.violations
    components.render_violations(violations)
    st.divider()
    _render_staffing_curve(result)

    with st.expander("🧾 最適化の詳細（変数・制約・目的関数）", expanded=False):
        rows = [{"項目": k, "値": v} for k, v in sorted((result.stats or {}).items())]
        if result.objective_value is not None:
            rows.append({"項目": "目的関数", "値": round(float(result.objective_value), 3)})
        st.dataframe(rows, hide_index=True, width="stretch", key="solve_stats")
        st.caption(
            f"割当セル数: {len(result.assignments):,} ／ "
            f"シフト日数: {len(result.shift_days)} ／ "
            f"利用可能ソルバ: {', '.join(state.get(state.KEY_SOLVER_NAMES)) or 'なし'}"
        )
    theme.caveat_box()
