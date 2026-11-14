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
    (0.05, "園の設定と条件を確認しています…"),
    (0.15, "配置基準をもとに必要人数を計算しています…"),
    (0.25, "シフトの条件（職員希望・勤務契約）を整理しています…"),
    (0.35, "AIエンジンで最適シフトを計算しています（数十秒かかる場合があります）…"),
    (0.80, "シフト表を組み立てています…"),
    (0.88, "勤務パターン（早番・日勤・遅番）に整列しています…"),
    (0.90, "配置基準や労働基準法の違反を点検しています…"),
    (1.00, "シフトが完成しました。"),
)


def build_requirements() -> Any:
    """タブ2 と同じ条件で ``RequirementTable`` を作り直す。"""
    from shiftai.ui import tab_requirements

    return tab_requirements.build_requirements()


def shortfall_gate(
    table: Any,
    staff: Any,
    preferences: Any,
    settings: Any,
    standard: Any,
) -> list:
    """需要>供給の (日,時間帯) を不足量順に返す（求解ゲート用）。"""
    return list(solver.shortfall_rows(staff, table, preferences, settings, standard))


def run_solve(*, force: bool = False, relaxation: int | None = None) -> SolveResult | None:
    """最適化を実行して結果を session_state に保存する。

    ``force`` が True のときは再計算を強制する。
    ``relaxation`` を渡すと、その緩和段階（上から緩める）で実行する。
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
    if not patterns:
        from shiftai.shift_patterns import default_patterns

        settings = state.current_settings()
        patterns = default_patterns(settings.day_open, settings.day_close)
    level = state.get(state.KEY_RELAXATION) if relaxation is None else relaxation
    if not children:
        raise ValueError("園児データが未投入です。")
    if not staff:
        raise ValueError("職員データが未投入です。")

    table = build_requirements()
    gaps = shortfall_gate(table, staff, preferences, settings, standard)
    ack_key = "shortfall_ack"
    if gaps and not state.get(ack_key):
        st.warning(
            f"⚠️ 需要が供給を上回る時間帯が {len(gaps)} 件あります。"
            "このまま求解すると不足は BLOCKER として報告されます。"
        )
        for g in gaps[:5]:
            st.caption(f"- {g.describe()}")
        agreed = st.checkbox("不足を理解したうえで求解を続行する", key="shortfall_ack_box")
        if not agreed:
            st.info("チェックを入れると求解を実行できます。")
            return None
        state.set(ack_key, True)

    limit = int(state.get(state.KEY_TIME_LIMIT_SEC))
    # 求解に失敗しても**前回の結果がそのまま出力可能なまま残らない**ようにする。
    # 失敗時に古い結果を残すと、エラーの出ている新しい計算と、
    # 「一番新しい結果」としてダウンロードできてしまう古いシフトが混在し、
    # ``pipeline_stage`` も ``STAGE_SOLVED`` のまま（= stale 印が付かない）になる。
    # 開始時点で破棄し、例外が出ても「結果なし」の状態に戻す。
    _clear_solve_outputs()
    progress = st.progress(0.0, text=PROGRESS_STEPS[0][1])
    try:
        progress.progress(PROGRESS_STEPS[0][0], text=PROGRESS_STEPS[0][1])
        state.set(state.KEY_REQUIREMENTS, table)
        progress.progress(PROGRESS_STEPS[1][0], text=PROGRESS_STEPS[1][1])
        progress.progress(PROGRESS_STEPS[2][0], text=PROGRESS_STEPS[2][1])
        with st.spinner(PROGRESS_STEPS[3][1]):
            solve_mode = st.session_state.get("solve_mode", "weekly")
            if solve_mode == "monthly":
                from shiftai.monthly import MonthlySolveContext, solve_monthly_hierarchical

                days_list = table.all_days()
                contracts_map = {s.staff_id: s.contract for s in staff if s.contract}
                m_ctx = MonthlySolveContext(
                    target_month=f"{days_list[0].year:04d}-{days_list[0].month:02d}" if days_list else "",
                    all_days=days_list,
                    staff=staff,
                    contracts=contracts_map,
                    preferences=preferences,
                    patterns=patterns or (),
                    requirements=table,
                    facility_settings=settings,
                    fixed_assignments=fixed,
                    prior_duty_history={},
                    monthly_holiday_target=int(st.session_state.get("monthly_holiday_target", 8)),
                    children=children,
                )
                result = solve_monthly_hierarchical(
                    m_ctx,
                    time_limit_sec=limit,
                )
            else:
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


def _clear_solve_outputs() -> None:
    """直近の求解結果を破棄する。

    新しい求解の開始時に呼ぶ。失敗時に前回の結果を残しておくと、
    「新しい計算のエラー」が出ながら「一番新しいシフト」として
    古いものがダウンロードできてしまう。
    """
    for key in (
        state.KEY_SOLVE_RESULT,
        state.KEY_GAP_REPORT,
        state.KEY_VIOLATIONS,
        state.KEY_PATTERN_SNAP_REPORT,
    ):
        state.reset(key)





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
    if not patterns:
        from shiftai.shift_patterns import default_patterns

        settings = state.current_settings()
        patterns = default_patterns(settings.day_open, settings.day_close)
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
    """実行前の前提確認（必要人数と職員の契約時間のバランス）。"""
    required = standards.total_required_hours(table)
    supply = state.contract_hours()
    ratio = state.supply_demand_ratio(required)
    shown = theme.format_ratio(ratio)
    
    # 画面上部には1行の要約ステータスのみを表示して情報量をスリム化
    if required <= 0:
        st.info("登降園予定が登録されていません。")
    elif supply <= 0 or ratio > 1.0:
        st.warning(
            f"⚠️ 職員の総契約時間（{supply:,.1f}h）が必要時間数（{required:,.1f}h）を下回っています（充足比: {shown}）。"
            "人員の追加や勤務可能時間の調整が必要な可能性があります。"
        )
    else:
        st.caption(
            f"💡 人員バランス見通し: 充足可能（必要 {required:,.1f}h ／ 供給可能 {supply:,.1f}h ・ 比率 {shown}）"
        )

    # 詳細な数値や内訳はアコーディオンに格納
    with st.expander(f"✅ 最適化の前提を確認する（充足比: {shown}）", expanded=False):
        components.metric_row(
            [
                ("必要時間数（総計）", f"{required:,.1f} h", "基準を満たす総人時"),
                (
                    "職員契約時間（総計）",
                    f"{supply:,.1f} h",
                    f"{len(state.get(state.KEY_STAFF) or [])} 名の契約時間合計",
                ),
                (
                    "充足比率（必要/供給）",
                    ("!" if ratio > 1.0 else "~") + shown,
                    "1.00超で全体不足",
                ),
                (
                    "ピーク必要人数",
                    f"{standards.peak_requirement(table)} 人",
                    "最も混雑する時間帯の人数",
                ),
                (
                    "計算時間上限",
                    f"{int(state.get(state.KEY_TIME_LIMIT_SEC))} 秒",
                    "最大計算時間",
                ),
            ]
        )
        if ratio > 1.0:
            st.error(
                f"必要時間数 {required:,.1f} h ＞ 供給可能時間 {supply:,.1f} h（比率 {shown}）。"
                "**職員数・契約時間・登園人数のいずれかを調整しないと、配置基準を満たすシフトは作成できません。**"
            )
        else:
            st.success(
                f"必要／供給比率は {shown} です。総時間としては足りています。"
                "（※各時間帯の出勤希望や勤務帯によって一部で過不足が発生する場合があります）"
            )
        st.caption(
            "※ 計算規模: 約 "
            f"{len(state.get(state.KEY_STAFF) or []) * len(table.slots) * len(table.all_days()):,} コマ。"
            "職員数や計画日数を増やすと計算時間が増加します。"
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
                "法令・基準抵触（要修正）",
                ("!%d 件" if result.blockers() else "%d 件") % len(result.blockers()),
                f"要調整 {summary.get('要調整件数', 0.0):.0f} 件",
            ),
            (
                "概算人件費",
                f"{cost:,.0f} 円",
                f"実働 × 1時間 {settings.labor_cost_per_hour:,.0f} 円 × 雇用形態係数",
            ),
            (
                "総勤務時間",
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
    """過不足テーブル（サマリー表示＋詳細アコーディオン）。"""
    report = state.get(state.KEY_GAP_REPORT)
    if report is None:
        return
    st.markdown("#### ⚖️ 過不足テーブル")
    frame = components.gap_table_frame(report)
    if frame.empty:
        st.info("比較する時間帯がありません。")
        return
    has_shortfall = bool(report.total_shortfall_slots and report.total_shortfall_slots > 0)
    components.metric_row(
        [
            ("不足時間帯", f"{report.total_shortfall_slots} 件", "必要人員に届かなかった枠"),
            ("不足時間数", f"{report.total_shortfall_hours:,.2f} h", "足りない総時間"),
            (
                "保育士不足",
                f"{report.total_qualified_shortfall_hours:,.2f} h",
                "有資格者不足の時間数",
            ),
            ("余剰時間数", f"{report.total_overstaff_hours:,.2f} h", "基準より多く配置した時間"),
            (
                "最大不足日",
                theme.format_day(report.worst_day) if report.worst_day else "—",
                "最も不足が集中した日",
            ),
        ]
    )
    # 情報過多を防ぐため、詳細データ表はアコーディオン式に格納（不足発生時のみ自動展開）
    exp_label = (
        f"時間帯ごとの詳細な過不足データ（⚠️ 不足 {report.total_shortfall_slots} 件あり）"
        if has_shortfall
        else "時間帯ごとの詳細な過不足データ（クリックで展開）"
    )
    with st.expander(exp_label, expanded=has_shortfall):
        st.dataframe(
            components.style_gap_table(frame),
            hide_index=True,
            width="stretch",
            height=320,
            key="gap_table",
        )
        heatmap_hint = ""
        if not theme.simple_mode():
            heatmap_hint = (
                f" 行数が多い場合は{theme.tab_ref(1)}の過不足ヒートマップも参照してください。"
            )
        st.caption(f"不足（赤）／過剰（青）を色で示しています。{heatmap_hint}")
        with st.expander("日別の人員サマリー", expanded=False):
            st.dataframe(
                report.daily_dataframe(),
                hide_index=True,
                width="stretch",
                key="gap_daily",
            )


def _render_staffing_curve(result: SolveResult) -> None:
    """必要人員と配置人員の重ね書き（人員配置曲線）。グラフ・数値表はアコーディオンに格納。"""
    st.markdown("#### 📈 人員配置曲線（必要人員 vs 配置人員）")
    table = state.get(state.KEY_REQUIREMENTS)
    staff = state.get(state.KEY_STAFF) or []
    days = components.days_of(result)
    if table is None or not days:
        st.info("比較できるデータがありません。")
        return
    with st.expander("人員配置グラフ・詳細数値表・職員別勤務時間（クリックで展開）", expanded=False):
        day = st.selectbox(
            "表示する日",
            options=days,
            format_func=theme.format_day,
            key="curve_day",
            help="時間帯ごとの必要人数と実際の配置人数の推移を表示する日付を選びます。",
        )
        frame = components.staffing_curve_frame(table, result, day, staff)
        if frame.empty:
            st.info("この日のデータがありません。")
            return
        chart = frame.set_index("時間帯")[["必要人員", "配置人員", "必要保育士数"]]
        st.line_chart(chart, height=280, width="stretch")
        st.caption("「必要人員」と「配置人員」の線が重なっていれば、配置基準を満たしています。")
        with st.expander("時間帯別の詳細数値表", expanded=False):
            styled = frame.set_index("時間帯").style.map(
                lambda v: (
                    f"background-color: {COLOR_SHORTFALL}33; color: {COLOR_SHORTFALL_INK}; font-weight:600;"
                    if str(v) and float(v) < 0
                    else ""
                ),
                subset=["過不足"],
            )
            st.dataframe(styled, width="stretch", key="curve_table")
        with st.expander("職員別の勤務時間グラフ・内訳", expanded=False):
            hours = components.staff_hours_frame(result, staff)
            if hours.empty:
                st.info("勤務時間データがありません。")
            else:
                st.bar_chart(
                    hours.set_index("職員ID")[["勤務時間"]],
                    height=240,
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
                    "※ 各職員の週あたり勤務時間が労働基準法の目安（40時間前後）に収まっているかご確認ください。"
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
                f"先に{theme.tab_ref(1)}（必要人員）で「必要人員を再計算」を押してください。"
            )
        theme.stuck_hint(2, "必要人員がまだ計算されていません。")
        return

    _render_precheck(table)
    _render_diagnosis(table, state.get(state.KEY_SOLVE_RESULT))

    col_m1, col_m2 = st.columns([3, 2])
    with col_m1:
        st.radio(
            "最適化モード",
            options=["weekly", "monthly"],
            format_func=lambda m: "週単位 (7日間)" if m == "weekly" else "月次一括 (2段階階層的最適化)",
            key="solve_mode",
            horizontal=True,
            help="1ヶ月などの長期シフトを作成する場合は「月次一括」を選択すると、公休目標や連勤制限を守りながら高速に解けます。",
        )
    with col_m2:
        if st.session_state.get("solve_mode") == "monthly":
            st.number_input(
                "月間公休目標日数",
                min_value=4,
                max_value=15,
                value=8,
                key="monthly_holiday_target",
                help="1人あたりの月間公休数の目標値（既定: 8日）",
            )

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

    st.markdown("#### ⚠️ 違反一覧")
    violations = state.get(state.KEY_VIOLATIONS) or result.violations
    blockers = result.blockers() if hasattr(result, "blockers") else []
    has_blockers = len(blockers) > 0
    v_title = (
        f"法令・配置基準の抵触詳細（🚨 要修正 {len(blockers)} 件 / 全 {len(violations)} 件）"
        if has_blockers
        else (f"運用上の確認事項（{len(violations)} 件）" if violations else "法令・配置基準の違反なし（0 件）")
    )
    with st.expander(v_title, expanded=has_blockers):
        components.render_violations(violations)
        components.render_violation_actions(violations, key_prefix="solve")

    components.render_fairness_panel(
        result,
        state.get(state.KEY_STAFF) or [],
        state.current_slots(),
        standard=state.current_standard(),
        patterns=state.current_patterns(),
    )
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
