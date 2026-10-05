"""タブ2「必要人員」: 配置基準エンジンから必要人員を計算して可視化する。"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import streamlit as st

from shiftai import gap_analysis, local_rules, standards
from shiftai.config import APP_TIME_INPUT_STEP_SECONDS
from shiftai.domain import HEADCOUNT_FACILITY_FORMULA, AgeClass, Role
from shiftai.ui import components, state, theme

LONG_COLUMN_CONFIG: dict[str, Any] = {
    "日付": st.column_config.DateColumn("日付", format="YYYY/MM/DD"),
    "時間帯": st.column_config.TextColumn("時間帯", width="small"),
    "開始": st.column_config.TimeColumn("開始", format="HH:MM", step=APP_TIME_INPUT_STEP_SECONDS),
    "終了": st.column_config.TimeColumn("終了", format="HH:MM", step=APP_TIME_INPUT_STEP_SECONDS),
    "年齢クラス": st.column_config.TextColumn("年齢クラス", width="small"),
    "在園児数": st.column_config.NumberColumn("在園児数", format="%d"),
    "必要人員": st.column_config.NumberColumn("必要人員", format="%d"),
    "必要保育士数": st.column_config.NumberColumn("必要保育士数", format="%d"),
    "時間帯区分": st.column_config.TextColumn("時間帯区分", width="medium"),
    "根拠": st.column_config.TextColumn("根拠", width="large"),
    "必須": st.column_config.CheckboxColumn("必須"),
}


def build_requirements() -> Any:
    """現在の session_state から ``RequirementTable`` を作り直す。"""
    settings = state.current_settings()
    standard = state.current_standard()
    days = state.current_days()
    children = state.get(state.KEY_CHILDREN) or []
    if not days:
        days = list(state.refresh_days(date.today(), 1))
    return standards.build_requirements(
        children,
        days,
        standard,
        day_open=settings.day_open,
        day_close=settings.day_close,
        granularity_min=settings.granularity_min,
        closed_days=settings.closed_days,
        holiday_dates=settings.holiday_dates,
        enforce_min_two=bool(state.get(state.KEY_ENFORCE_MIN_TWO)),
    )


def compute_requirements() -> None:
    """``build_requirements`` を実行して session_state に保存する。"""
    table = build_requirements()
    state.set(state.KEY_REQUIREMENTS, table)
    state.reset(state.KEY_SOLVE_RESULT)
    state.reset(state.KEY_GAP_REPORT)
    state.reset(state.KEY_VIOLATIONS)


def _ratio_label(ratio: float) -> str:
    """必要／供給比の表示。

    ``state.supply_demand_ratio`` は供給 0・需要 >0 のとき ``inf`` を返す。
    ``"%.2f" % inf`` は ``"inf"`` になるため、そのまま整形すると
    ``!inf`` という無意味なカード、および「``!`` が tone 判定に使われる」
    ``components.metric_html`` の挙動を招く（``tab_solve._ratio_text`` は
    ``inf`` / ``nan`` を明示的に扱うので、この呼び出しだけが漏れていた）。
    """
    if math.isinf(ratio) or math.isnan(ratio):
        return "充足不能"
    return ("!%.2f" if ratio > 1.0 else "~%.2f") % ratio


def _render_kpis(table: Any) -> None:
    """KPI カード（ピーク必要人員・総必要人時・園児数・職員数）。"""
    standard = state.current_standard()
    settings = state.current_settings()
    children = state.get(state.KEY_CHILDREN) or []
    staff = state.get(state.KEY_STAFF) or []
    peak = standards.peak_requirement(table)
    total_hours = standards.total_required_hours(table)
    ratio = state.supply_demand_ratio(total_hours)
    components.metric_row(
        [
            (
                "ピーク必要人員",
                f"{peak} 人",
                "最も混雑する時間帯（1日あたり）",
            ),
            (
                "総必要人時",
                f"{total_hours:,.1f} h",
                f"{len(table.all_days())} 日 × {len(table.slots)} 時間帯",
            ),
            ("園児数", f"{len(children)} 名", f"{settings.facility_name}"),
            (
                "職員数",
                f"{len(staff)} 名",
                f"供給 {state.contract_hours():,.0f} h（契約時間ベース）",
            ),
            (
                "必要／供給 比",
                _ratio_label(ratio),
                "1.00 を超えると構造的に基準を満たせません",
            ),
        ]
    )
    st.caption(
        f"適用基準: {standard.name} ／ 最低配置 {standard.min_staff_per_room} 名"
        f"（2名ルール {'有効' if state.get(state.KEY_ENFORCE_MIN_TWO) else '無効'}）"
        f" ／ 休憩 {standard.break_minutes} 分"
    )


def _render_heatmap(table: Any) -> None:
    """時間帯×年齢クラスのヒートマップ。"""
    st.markdown("#### 🧮 時間帯 × 年齢クラスの必要人員")
    days = table.all_days()
    if not days:
        st.info("開園日がないため必要人員を計算できませんでした。")
        return
    left, right = st.columns([1, 2])
    with left:
        day = st.selectbox(
            "対象日",
            options=days,
            format_func=theme.format_day,
            key="requirements_day",
        )
    with right:
        st.caption(
            "行＝年齢クラス、列＝時間帯。右端と下端の「合計」はその年齢クラス／その時間帯の"
            "同時必要人員の合計です（時間帯ごとの合計は異なる日に重複しないため合算しません）。"
        )
    frame = components.requirement_heatmap(table, day)
    if frame.empty:
        st.info("この日は在園児がいないか休園日です。")
        return
    styled = components.heat_styler(frame, scale="warm", subset=list(frame.columns[:-1]))
    st.dataframe(styled, width="stretch", key="requirement_heatmap")

    slot_kinds: dict[str, int] = {}
    for requirement in table.for_day(day):
        slot_kinds[requirement.slot.label] = requirement.slot_kind
    standard = state.current_standard()
    with st.expander("時間帯区分ごとの必要人員", expanded=False):
        records = []
        for slot in table.slots:
            kind = slot_kinds.get(slot.label)
            if kind is None:
                kind = standard.slot_kind(slot)
            records.append(
                {
                    "時間帯": slot.label,
                    "区分": standards.slot_kind_label(kind),
                    "必要人員": table.needed_staff(day, slot),
                    "必要保育士数": table.needed_qualified(day, slot),
                }
            )
        st.dataframe(
            records,
            hide_index=True,
            width="stretch",
            key="slot_kind_breakdown",
        )
        st.caption(
            "早朝保育="
            f"{_window(standard.early_care_window)}、保育標準時間="
            f"{_window(standard.standard_time)}、延長保育="
            f"{_window(standard.late_care_window)}"
        )


def _window(window: tuple[Any, Any]) -> str:
    return f"{window[0].strftime('%H:%M')}-{window[1].strftime('%H:%M')}"


def _render_long_table(table: Any) -> None:
    """1 日分の必要人員×在園児数の内訳表と根拠。"""
    st.markdown("#### 📋 必要人員の内訳（在園児数 → 必要人員）")
    days = table.all_days()
    if not days:
        st.info("表示できる日がありません。")
        return
    day = st.selectbox(
        "内訳を表示する日",
        options=days,
        format_func=theme.format_day,
        key="requirements_long_day",
    )
    frame = table.to_long_dataframe()
    if not frame.empty:
        day_text = day.isoformat()
        subset = frame[frame["日付"].astype(str) == day_text]
        st.dataframe(
            subset,
            hide_index=True,
            width="stretch",
            column_config=LONG_COLUMN_CONFIG,
            key="requirements_long",
        )
        st.caption(f"{len(subset)} 行（{theme.format_day(day)} の内訳）")
    with st.expander("根拠（配置基準からの導出過程）", expanded=False):
        rows = components.requirement_basis_rows(table, day)
        if not rows:
            st.info("この日に必要人員はありません。")
        for heading, explanation in rows:
            st.markdown(f"- **{heading}** — {explanation}")
        for note in table.notes:
            st.caption(f"ℹ️ {note}")


def _render_preset_comparison() -> None:
    """プリセット比較表。"""
    with st.expander("📚 配置基準プリセットの比較（全自治体）", expanded=False):
        theme.caveat_box()
        frame = local_rules.standard_to_dataframe()
        st.dataframe(
            frame,
            hide_index=True,
            width="stretch",
            key="preset_comparison",
        )
        st.caption(
            "この表はあくまで参考値です。実際の基準は所轄自治体の告示・条例を必ず確認してください。"
        )


def _render_gap_preview(table: Any) -> None:
    """タブ3 の実行結果があれば過不足ヒートマップも表示する。"""
    result = state.get(state.KEY_SOLVE_RESULT)
    if result is None or not result.shift_days:
        with st.expander(
            f"⚖️ 過不足ヒートマップ（{theme.tab_ref(2)} の実行後に表示）", expanded=False
        ):
            st.info(
                f"まだシフトを作成していません。{theme.tab_ref(2)}（シフト作成）で"
                "最適化を実行してください。"
            )
        return
    st.markdown("#### ⚖️ 過不足（必要人員 − 配置人員）")
    frame = gap_analysis.gap_matrix(table, result)
    if frame.empty:
        st.info("比較できるデータがありません。")
        return
    styled = components.heat_styler(
        frame, scale="shortfall", vmin=-3.0, vmax=3.0, subset=list(frame.columns[1:])
    )
    st.dataframe(styled, width="stretch", key="gap_matrix")
    st.caption(
        f"赤＝不足（負）、緑＝充足（正）。"
        f"この行列は{theme.tab_ref(2)}の最適化結果から計算しています。"
    )


def _ratio_text(ratio: float) -> str:
    """必要／供給比を表示用文字列にする（``inf`` は「∞」表記）。"""
    if ratio == float("inf"):
        return "∞"
    return f"{ratio:.2f}"


def _render_supply_warning(table: Any) -> float:
    """必要人時と供給可能人時の比による事前警告。戻り値は比率（``inf`` あり）。"""
    required = standards.total_required_hours(table)
    supply = state.contract_hours()
    ratio = state.supply_demand_ratio(required)
    shown = _ratio_text(ratio)
    with st.expander("✅ 必要人員の計算結果（内訳）", expanded=False):
        st.markdown(f"- 総必要人時: **{required:,.1f} h**（{len(table.all_days())} 日）")
        st.markdown(f"- 供給可能人時（契約時間ベース）: **{supply:,.1f} h**")
        st.markdown(f"- 必要／供給 比: **{shown}**")
    if required <= 0:
        st.info("必要人時が 0 h です。園児の登降園予定が登録されていません。")
    elif supply <= 0:
        st.error(
            f"供給可能人時が **{supply:,.1f} h** です。必要人時 {required:,.1f} h を"
            "満たす人員が 1 人も配置できません（比 = ∞）。"
            "職員の契約時間帯（最早始業／最遅終業）や園の開所時間、"
            "休園日の設定を確認してください。"
        )
    elif ratio > 1.0:
        st.error(
            f"必要人時 {required:,.1f} h が供給可能人時 {supply:,.1f} h を超えています"
            f"（比 {shown}）。**どの組み合わせでも配置基準を満たせません**。"
            "職員を増やす・週契約時間を増やす・園児数を減らす・基準を見直すのいずれかが必要です。"
        )
    elif ratio > 0.9:
        st.warning(
            f"必要／供給比が {shown} です。勤務日数・希望休の制約を考えると"
            "時間どおりに配置できない時間帯が出る可能性があります。"
        )
    else:
        st.success(f"必要／供給比は {shown} で、総人時としては基準を満たせる状態です。")
    return ratio


def _render_ratio_summary() -> None:
    """適用中の定員比を一覧表示する。"""
    standard = state.current_standard()
    st.markdown("#### 📐 適用中の定員比")
    records = []
    for age_class in sorted(AgeClass, key=lambda a: a.sort_key):
        try:
            ratio = standard.ratio_for(age_class)
        except KeyError:
            continue
        records.append(
            {
                "年齢クラス": age_class.value,
                "定員比": standards.ratio_label(standard, age_class),
                "丸め": {
                    "ceil": "切り上げ",
                    "floor": "切り捨て",
                    "round": "四捨五入",
                    "trunc1": "小数第2位以下切り捨て",
                }.get(ratio.rounding, ratio.rounding),
            }
        )
    left, right = st.columns([1, 1])
    with left:
        st.dataframe(records, hide_index=True, width="stretch", key="ratio_summary")
    with right:
        st.markdown(
            f"- 保育標準時間: {_window(standard.standard_time)}\n"
            f"- 早朝保育: {_window(standard.early_care_window)}\n"
            f"- 延長保育: {_window(standard.late_care_window)}\n"
            f"- 延長時の保育士代替: {'可' if standard.late_care_relaxed else '不可'}"
            f"（最低保育士 {standard.late_care_min_qualified} 名）\n"
            f"- 短時間保育のみ園: {'はい' if standard.is_short_time_only else 'いいえ'}\n"
            f"- 必要人員の算手法: {local_rules.headcount_mode_label(standard)}\n"
            f"- 必要保育士数の決め方: {local_rules.qualified_mode_label(standard)}"
            f"（下限 {standard.min_qualified_floor} 名）\n"
            f"- 看護師のみなし保育士: "
            + (
                f"{standard.nurse_as_qualified_cap} 名まで"
                if standard.nurse_as_qualified_cap > 0
                else (
                    "比率の分子にそのまま計上"
                    if standard.qualified_extra_roles & {Role.KANGSHI}
                    else "数えない"
                )
            )
            + "\n"
            f"- 備考: {standard.remarks or '—'}"
        )


def _render_compliance(standard: Any) -> None:
    """制度別の適合チェック（届出・月次報告・巡回指導の場面用）。

    シフト作成の判定（:mod:`shiftai.gap_analysis`）とは別物で、
    「その時間帯の在園人数」ではなく**1日の常勤換算人数と施設の属性**で判定する。
    """
    from shiftai import compliance as compliance_mod

    st.markdown("#### 🧾 制度適合チェック")
    regulation_keys = [r.value for r in compliance_mod.Regulation]
    columns = st.columns([2, 1, 1, 1])
    with columns[0]:
        regulation_name = st.selectbox(
            "制度",
            options=regulation_keys,
            index=regulation_keys.index(
                "認可外保育施設"
                if standard.headcount_mode == HEADCOUNT_FACILITY_FORMULA
                else "認可保育所"
            ),
            key="compliance_regulation",
            help="届出・報告に使う制度。算手法の整合もここで確認します。",
        )
    with columns[1]:
        capacity = int(
            st.number_input(
                "利用定員", min_value=0, max_value=500, value=60, step=1, key="compliance_capacity"
            )
        )
    with columns[2]:
        shared = st.checkbox(
            "保育事業者型",
            value=False,
            key="compliance_shared",
            help="共同利用枠を実施している場合、保育士が4分の3以上必要です。",
        )
    with columns[3]:
        support_certified = st.checkbox(
            "支援員は研修修了",
            value=False,
            key="compliance_support_certified",
            help="子育て支援員研修（地域型）修了者・市町村研修受講予定者として扱います。",
        )

    settings = state.current_settings()
    children = state.get(state.KEY_CHILDREN) or []
    staff = state.get(state.KEY_STAFF) or []
    monthly: dict[AgeClass, int] = {}
    seen: set[str] = set()
    for plan in children:
        if plan.child_id in seen:
            continue
        seen.add(plan.child_id)
        monthly[plan.age_class] = monthly.get(plan.age_class, 0) + 1

    spec = compliance_mod.FacilitySpec(
        regulation=compliance_mod.Regulation(regulation_name),
        name=str(settings.facility_name or ""),
        capacity=capacity,
        is_shared_operator=bool(shared),
        opening=settings.day_open,
        closing=settings.day_close,
        monthly_children=monthly,
        staff=tuple(
            compliance_mod.StaffRecord(
                staff_id=member.staff_id,
                name=member.name,
                roles=member.roles,
                weekly_hours=member.contract.weekly_hours,
                is_certified=(
                    True
                    if (
                        member.is_qualified_under(None)
                        or member.has_role(Role.CHUUBOU)
                        or support_certified
                    )
                    else None
                ),
            )
            for member in staff
            if member.is_placeable or member.has_role(Role.CHUUBOU)
        ),
        standard=standard,
    )
    try:
        report = compliance_mod.audit_facility(spec)
    except ValueError as exc:
        st.info(f"この制度のプリセットは自動選択できません。{exc}")
        return

    if report.violations:
        st.error(f"不適合 {len(report.violations)} 件があります。")
    elif report.is_filing_ready:
        st.success(report.summary())
    else:
        st.warning(report.summary())
    st.dataframe(
        compliance_mod.to_dataframe(report),
        hide_index=True,
        width="stretch",
        column_config={
            "判定": st.column_config.TextColumn("判定", width="small"),
            "根拠": st.column_config.TextColumn("根拠", width="medium"),
        },
        key="compliance_table",
    )
    st.caption(
        "未確認（未入力）は不適合には数えませんが、報告・申請の前に人が判断する必要があります。"
        "面積は園舎図面が必要です。嘱託医と調理業務の委託形態は仕様から読み取れません。"
    )


def render() -> None:
    """タブ2 の本体。"""
    theme.step_indicator(1)
    theme.heading(1)
    if not state.data_ready():
        theme.empty_state()
        if state.staff_missing():
            theme.stuck_hint(1, "職員データが 0 名です。")
        else:
            theme.stuck_hint(1, "園児データが 0 名です。")
        return
    days = state.current_days()
    if not days:
        st.warning(theme.where_is_period())
        theme.stuck_hint(1, "計画期間が設定されていません。")
        return

    left, right = st.columns([1, 2])
    with left:
        if st.button(
            "🔄 必要人員を再計算",
            type="primary",
            width="stretch",
            key="recompute_requirements",
        ):
            try:
                with st.spinner("配置基準エンジンで計算しています…"):
                    compute_requirements()
                st.success("必要人員を計算しました。")
            except Exception as exc:  # noqa: BLE001 - 計算失敗で画面を落とさない
                st.error(f"必要人員の計算に失敗しました: {exc}")
    with right:
        st.caption(
            "園設定・配置基準・計画期間・2名ルールを変えたら、必ずこのボタンを押してください。"
            "閉じているタブのウィジェットは押さないと反映されません。"
        )

    table = state.get(state.KEY_REQUIREMENTS)
    if table is None:
        st.info("「必要人員を再計算」を押してください。")
        theme.stuck_hint(1, "必要人員がまだ計算されていません。")
        return
    if not table.notes:
        pass
    else:
        with st.expander(f"ℹ️ エンジンからの注記（{len(table.notes)} 件）", expanded=True):
            for note in table.notes:
                st.caption(f"- {note}")

    _render_kpis(table)
    _render_supply_warning(table)
    st.divider()
    _render_ratio_summary()
    st.divider()
    with st.expander("🧾 制度適合チェック（届出・月次報告向け）", expanded=False):
        _render_compliance(state.current_standard())
    st.divider()
    _render_heatmap(table)
    st.divider()
    _render_long_table(table)
    st.divider()
    _render_gap_preview(table)
    st.divider()
    _render_preset_comparison()
    theme.caveat_box()
    theme.next_step_hint(1)
