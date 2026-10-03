"""タブ3「シフト自動作成」: PuLP（CBC）による最適化の実行と結果表示。"""

from __future__ import annotations

from typing import Any

import streamlit as st

from shiftai import diagnostics, gap_analysis, solver, standards
from shiftai.config import COLOR_SHORTFALL, COLOR_SHORTFALL_INK
from shiftai.domain import SolveResult
from shiftai.relaxation import (
    RELAX_LEVELS,
    relax_level_index,
    relaxation_options,
    relaxed_constraint_labels,
)
from shiftai.ui import components, state, theme

PROGRESS_STEPS: tuple[tuple[float, str], ...] = (
    (0.05, "設定を検証しています…"),
    (0.15, "配置基準エンジンで必要人員を再計算しています…"),
    (0.25, "最適化モデルを構築しています（変数・制約の生成）…"),
    (0.35, "PuLP（CBC）で最適化しています。この処理は数十秒かかることがあります…"),
    (0.80, "解をデコードしてシフト表にしています…"),
    (0.88, "勤務パターンの境界へ整列しています…"),
    (0.90, "過不足と法令違反を検証しています…"),
    (1.00, "完了しました。"),
)


def build_requirements() -> Any:
    """タブ2 と同じ条件で ``RequirementTable`` を作り直す。"""
    from shiftai.ui import tab_requirements

    return tab_requirements.build_requirements()


def run_solve(*, force: bool = False, relaxation: int | None = None) -> SolveResult | None:
    """最適化を実行して結果を session_state に保存する。

    ``force`` が True のときは再計算を強制する。
    ``relaxation`` を渡すと、その緩和段階（上から缓める）で実行する。
    例外は呼び出し側で表示する。
    """
    settings = state.current_settings()
    standard = state.current_standard()
    children = state.get(state.KEY_CHILDREN) or []
    staff = state.get(state.KEY_STAFF) or []
    preferences = state.get(state.KEY_PREFERENCES) or {}
    weights = state.sync_weights()
    fixed = state.normalize_fixed(state.get(state.KEY_FIXED_ASSIGNMENTS))
    patterns = state.current_patterns()
    level = state.get(state.KEY_RELAXATION) if relaxation is None else relaxation
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
                patterns=patterns,
                relaxation=level,
            )
        progress.progress(PROGRESS_STEPS[4][0], text=PROGRESS_STEPS[4][1])
        snap_changes: tuple = ()
        if patterns and state.get(state.KEY_PATTERN_SNAP):
            progress.progress(PROGRESS_STEPS[5][0], text=PROGRESS_STEPS[5][1])
            result, snap_changes = solver.snap_to_patterns(
                result,
                patterns,
                requirements=table,
                preferences=preferences,
                staff=staff,
                standard=standard,
                settings=settings,
            )
        report = gap_analysis.analyze_gap(table, result, staff, standard=standard)
        progress.progress(PROGRESS_STEPS[6][0], text=PROGRESS_STEPS[6][1])
        violations = gap_analysis.check_violations(
            table, result, staff, preferences, standard=standard, settings=settings
        )
        progress.progress(PROGRESS_STEPS[7][0], text=PROGRESS_STEPS[7][1])
    finally:
        progress.empty()

    state.set(state.KEY_SOLVE_RESULT, result)
    state.set(state.KEY_GAP_REPORT, report)
    state.set(state.KEY_VIOLATIONS, violations)
    state.set(state.KEY_PATTERN_SNAP_REPORT, snap_changes or None)
    state.set(state.KEY_RELAXATION, level)
    state.clear_stale()
    # いまの入力と結果が揃ったので、次の変更を検知できる基準を記録する。
    state.mark_inputs_baseline()
    st.session_state.pop("shift_editor", None)
    st.session_state.pop("gap_editor", None)
    st.session_state.pop("_fix_slot_label", None)
    return result


def _ratio_text(ratio: float) -> str:
    """必要／供給比を表示用文字列にする（``inf`` は「∞」表記）。"""
    if ratio == float("inf"):
        return "∞"
    return f"{ratio:.2f}"


def _render_diagnosis(table: Any, result: SolveResult | None) -> None:
    """「なぜ解けないのか」を説明し、緩和モードでの再実行を提供する。"""
    staff = state.get(state.KEY_STAFF) or []
    preferences = state.get(state.KEY_PREFERENCES) or {}
    report = diagnostics.diagnose(table, staff, preferences, state.current_settings())
    state.set(state.KEY_DIAGNOSIS, report)
    level = int(state.get(state.KEY_RELAXATION))

    with st.expander("🔍 なぜ解けないのか（原因の特定と緩和モード）", expanded=False):
        st.write(report.headline())
        if report.rows:
            st.dataframe(
                diagnostics.shortfall_dataframe(report),
                hide_index=True,
                width="stretch",
                key="diagnosis_shortfall",
            )
            st.caption(
                "「供給人員」は契約時間帯内で希望休でもない職員数です。"
                "この数が「必要人員」を下回っている行は、職員を増やさない限り埋まりません。"
            )
        for reason in report.reasons:
            st.warning(f"**{reason.label}**: {reason.message}\n\n対応: {reason.advice}")
        if not report.rows and not report.reasons:
            st.info(report.note)

        st.divider()
        st.markdown("**緩め方（緩和モード）**")
        options = relaxation_options()
        chosen = st.selectbox(
            "どこまで緩めて解けば解けるか",
            options=options,
            index=relax_level_index(level),
            key="relaxation_select",
            help=RELAX_LEVELS[level].description,
        )
        chosen_level = RELAX_LEVELS[options.index(chosen)].value
        st.caption(RELAX_LEVELS[chosen_level].description)
        if chosen_level > 0:
            st.warning(
                "緩めた制約: "
                + "、".join(relaxed_constraint_labels(chosen_level))
                + "。法令の上限時間（1日10時間）は緩めません。"
            )
        if st.button(
            "この設定で再実行する",
            key="rerun_with_relaxation",
            width="stretch",
        ):
            try:
                with st.spinner("緩和モードで最適化しています…"):
                    run_solve(force=True, relaxation=chosen_level)
                st.success(
                    f"L{chosen_level}（{RELAX_LEVELS[chosen_level].label}）で再実行しました。"
                )
            except Exception as exc:  # noqa: BLE001 - 最適化失敗で画面を落とさない
                st.error(f"再実行に失敗しました: {exc}")

        if st.button(
            "どの段階で解けるかを調べる（時間を要します）",
            key="run_relaxation_ladder",
            width="stretch",
            help="L0 から順に解いていき、初めて解けた段階を報告します。"
            "ソルバを複数回起動するため数十秒〜数分かかります。",
        ):
            ladder = _run_ladder(table)
            state.set(state.KEY_DIAGNOSIS_LADDER, ladder)
            for line in ladder.messages():
                st.caption(f"- {line}")

        with st.expander("制約グループを 1 つずつ外して原因を絞る（IIS）", expanded=False):
            st.caption(
                "各制約グループを 1 つだけ無効化して解き直し、"
                "「外しても解けないもの」を捨てます。残ったものが矛盾の当事者です。"
                "ソルバを制約グループ数だけ起動するため数分かかる場合があります。"
            )
            if st.button("原因を絞り込む", key="run_iis", width="stretch"):
                with st.spinner("制約グループを 1 つずつ外して検証しています…"):
                    core = diagnostics.conflict_core_report(
                        state.get(state.KEY_CHILDREN) or [],
                        staff,
                        table,
                        preferences,
                        settings=state.current_settings(),
                        standard=state.current_standard(),
                        time_limit_sec=max(8, int(state.get(state.KEY_TIME_LIMIT_SEC)) // 3),
                    )
                if len(core) == len(diagnostics.CONSTRAINT_GROUPS):
                    st.warning(
                        "どの制約グループを 1 つ外しても解けませんでした。"
                        "1 つの制約ではなく、**複数の制約の組合せ**が矛盾を作っています。"
                    )
                else:
                    st.error(f"矛盾の当事者は {len(core)} 件の制約グループです。")
                st.dataframe(
                    [{"制約": g.label, "確認のしかた": g.detail} for g in core],
                    hide_index=True,
                    width="stretch",
                    key="iis_result",
                )

    _render_pattern_panel(result)


def _run_ladder(table: Any) -> Any:
    """緩和ラダーを実行する（利用者がボタンを押したときだけ呼ぶ）。"""
    staff = state.get(state.KEY_STAFF) or []
    children = state.get(state.KEY_CHILDREN) or []
    preferences = state.get(state.KEY_PREFERENCES) or {}
    with st.spinner("緩和モードを順に検証しています…"):
        return diagnostics.relaxation_ladder(
            children,
            staff,
            table,
            preferences,
            settings=state.current_settings(),
            standard=state.current_standard(),
            time_limit_sec=max(10, int(state.get(state.KEY_TIME_LIMIT_SEC)) // 2),
        )


def _render_pattern_panel(result: SolveResult | None) -> None:
    """勤務パターンの設定状況と整列結果を示す。"""
    patterns = state.current_patterns()
    with st.expander("⏰ 勤務パターンへの整列（早番・日勤・遅番）", expanded=False):
        if not patterns:
            st.info(
                "勤務パターンは無効です。サイドバー「勤務パターン（早番・日勤・遅番）」で"
                "有効にすると、勤務ブロックの開始・終了をその境界へ引き寄せます。"
            )
            return
        st.markdown("**定義中のパターン**")
        st.dataframe(
            [
                {
                    "パターン": p.label,
                    "時間帯": p.span(),
                    "長さ": f"{p.hours:.1f} h",
                }
                for p in patterns
            ],
            hide_index=True,
            width="stretch",
            key="pattern_defs",
        )
        if result is None:
            return
        counts = solver.pattern_breakdown(result, patterns)
        if not counts:
            return
        st.markdown("**生成結果の内訳（勤務日数）**")
        st.dataframe(
            [{"区分": k, "日数": v} for k, v in sorted(counts.items())],
            hide_index=True,
            width="stretch",
            key="pattern_counts",
        )
        aligned = sum(v for k, v in counts.items() if k != "その他")
        total = sum(counts.values())
        ratio = (aligned / total) if total else 0.0
        components.metric_row(
            [
                ("パターン一致", f"{aligned} 日", f"全 {total} 勤務日中"),
                ("整列率", f"{ratio * 100:.0f}%", "早番・日勤・遅番の枠と一致した日数"),
                (
                    "その他",
                    f"{counts.get('その他', 0)} 日",
                    "契約や希望休のため枠に寄せられなかった日",
                ),
            ]
        )
        snap = state.get(state.KEY_PATTERN_SNAP_REPORT)
        if snap:
            moved = [c for c in snap if c.applied]
            st.caption(
                f"境界の整列を {len(moved)} 日分適用しました"
                f"（{len(snap) - len(moved)} 日分は契約・希望休・配置基準のため据え置き）。"
            )
            with st.expander("整列の内訳", expanded=False):
                st.dataframe(
                    [c.to_dict() for c in snap],
                    hide_index=True,
                    width="stretch",
                    key="pattern_snap_detail",
                )
        days = components.days_of(result)
        if days:
            day = st.selectbox(
                "勤務枠の内訳を見る日",
                options=days,
                format_func=theme.format_day,
                key="pattern_detail_day",
            )
            rows = []
            for sid in sorted({a.staff_id for a in result.assignments}):
                text = solver.describe_shift_pattern(result, sid, day, patterns)
                if text == "オフ":
                    continue
                rows.append({"職員ID": sid, "勤務枠": text})
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch", key="pattern_detail")


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
                "**職員数・契約時間・園児数のいずれかを調整しないと"
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
            "職員数や日数を増やすと最適化時間が大きく伸びます。"
        )


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
    heatmap_hint = ""
    if not theme.simple_mode():
        heatmap_hint = (
            f" 行数が多い場合は{theme.tab_ref(1)}の過不足ヒートマップも参照してください。"
        )
    st.caption(f"不足（赤）／過剰（青）を色で示しています。{heatmap_hint}")
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
                f"background-color: {COLOR_SHORTFALL}33; color: {COLOR_SHORTFALL_INK}; font-weight:600;"
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


def _render_infeasible_hint(result: SolveResult) -> None:
    """解なし・実行不能のとき、「どの緩和で解けるか」を調べる動線を示す。

    ステータスが「解なし」の場合、放置するとユーザーは何をしたらよいか分からず
    同じ設定で何度も押してしまう。緩和モードと原因診断はこのタブ内に
    すでにあるため、そこへ誘導する。
    """
    from shiftai.domain import SolveStatus

    if result.status not in (SolveStatus.INFEASIBLE, SolveStatus.ERROR):
        return
    with st.expander("🛠 解が見つかりません。対処方法", expanded=True):
        st.markdown("制約が互いに矛盾しています。次の順に試してください。")
        st.markdown(
            "1. 下の **「緩め方（緩和モード）」** から、基準を少しずつ緩めた設定で再実行する"
        )
        st.markdown("2. **「どの段階で解けるかを調べる」** を押して、衝突している制約を特定する")
        st.markdown(
            "3. 職員数・週契約時間・園児数を調整する"
            "（サイドバーの「⚙️ 詳細設定（上級者向け）」を開く）"
        )


def render(*, auto_requirements: bool = False, embed_shift: bool = False) -> None:
    """タブ3 の本体。

    ``auto_requirements=True``（シンプルモード）は、タブ2「必要人員」が
    画面上に無い代わりに、必要人員を自動的に計算してから実行ボタンを出す。

    ``embed_shift=True``（シンプルモード）は、結果表示の末尾に
    シフト表セクションを続けて描画する。3 タブ構成の「② シフト作成」の中に
    日別のシフト表と微調整を置き、タブを離れずに確認できるようにする。
    """
    theme.step_indicator(2)
    theme.heading(2)
    if not state.data_ready():
        theme.empty_state()
        if state.staff_missing():
            theme.stuck_hint(2, "職員データが 0 名です。")
        else:
            theme.stuck_hint(2, "園児データが 0 名です。")
        return
    table = state.get(state.KEY_REQUIREMENTS)
    if table is None and auto_requirements:
        try:
            with st.spinner("必要人員を計算しています…"):
                table = build_requirements()
            state.set(state.KEY_REQUIREMENTS, table)
        except Exception as exc:  # noqa: BLE001 - 計算失敗で画面を落とさない
            st.error(f"必要人員の計算に失敗しました: {exc}")
            return
    if table is None:
        if theme.simple_mode():
            # シンプルモードには「必要人員」タブが無い。自動計算が
            # 失敗している場合なので、データ側を見るよう案内する。
            st.warning(
                "必要人員がまだ計算されていません。"
                f"{theme.tab_ref(0)}（データ）を確認してから、もう一度シフトを作成してください。"
            )
        else:
            st.warning(
                f"先に{theme.tab_ref(1)}（必要人員）で"
                "「必要人員を再計算」を押してください。"
            )
        theme.stuck_hint(2, "必要人員がまだ計算されていません。")
        return

    _render_precheck(table)
    _render_diagnosis(table, state.get(state.KEY_SOLVE_RESULT))

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
                    st.write(f"ステータス: **{getattr(result.status, 'value', result.status)}**")
                status.update(label="最適化が完了しました", state="complete")
            st.success("シフトを作成しました。")
        except Exception as exc:  # noqa: BLE001 - 最適化の失敗で画面を落とさない
            st.error(f"シフトの作成に失敗しました: {exc}")

    render_result_section(embed_shift=embed_shift)


def render_result_section(*, embed_shift: bool = False) -> None:
    """最適化結果の表示（バナー・KPI・過不足・違反・公平性・配置曲線）。

    ``embed_shift=True`` のとき、末尾にシフト表セクション
    （``tab_shift.render_section``）を続けて描画する。シンプルモードでは
    「② シフト作成」の中にシフト表が見える。

    ``tab_shift`` はモジュールレベルで import しない。``tab_shift`` 側が
    このモジュールを関数内ローカル import しているので、逆方向も
    関数内ローカルにしないと循環する。
    """

    result = state.get(state.KEY_SOLVE_RESULT)
    if result is None:
        st.info("「シフトを自動作成する」を押すと、ここに結果が表示されます。")
        theme.stuck_hint(2, "シフトがまだ作成されていません。")
        return

    st.divider()
    components.status_banner(result)
    _render_infeasible_hint(result)
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
    components.render_violation_actions(violations, key_prefix="solve")
    st.divider()
    components.render_fairness_panel(
        result,
        state.get(state.KEY_STAFF) or [],
        state.current_slots(),
        standard=state.current_standard(),
        patterns=state.current_patterns(),
    )
    st.divider()
    _render_staffing_curve(result)

    with st.expander("🧾 最適化の詳細（変数・制約・目的関数）", expanded=False):
        st.dataframe(
            components.solve_stats_frame(result),
            hide_index=True,
            width="stretch",
            key="solve_stats",
        )
        st.caption(
            f"割当セル数: {len(result.assignments):,} ／ "
            f"シフト日数: {len(result.shift_days)} ／ "
            f"利用可能ソルバ: {', '.join(state.get(state.KEY_SOLVER_NAMES)) or 'なし'}"
        )
    theme.caveat_box()
    theme.next_step_hint(2)
    if embed_shift:
        from shiftai.ui import tab_shift

        tab_shift.render_section(in_simple=True)
