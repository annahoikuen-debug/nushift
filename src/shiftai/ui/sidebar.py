"""サイドバー（園設定・配置基準プリセット・計画期間・最適化オプション・GAS連携）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import streamlit as st

from shiftai import gas_client as gas
from shiftai import local_rules
from shiftai.config import (
    APP_ICON,
    COLOR_BREAK,
    COLOR_OFF,
    COLOR_WORK,
    DEFAULT_DAY_CLOSE,
    DEFAULT_DAY_OPEN,
    DEFAULT_GRANULARITY_MIN,
    DEFAULT_RANGE_DAYS,
    DEFAULT_RANGE_START,
    STATUTORY_WEEKLY_WORK_HOURS,
)
from shiftai.domain import AgeClass, FacilitySettings, daterange
from shiftai.ui import state, theme

GRANULARITIES: tuple[int, ...] = (15, 30, 60)
AGE_CLASSES: tuple[AgeClass, ...] = (
    AgeClass.INFANT,
    AgeClass.AGE_1,
    AgeClass.AGE_2,
    AgeClass.AGE_3,
    AgeClass.AGE_4,
    AgeClass.AGE_5,
)
DEFAULT_SETTINGS = FacilitySettings(
    day_open=DEFAULT_DAY_OPEN,
    day_close=DEFAULT_DAY_CLOSE,
    granularity_min=DEFAULT_GRANULARITY_MIN,
)


def _reset_ratio_widgets() -> None:
    for age_class in AGE_CLASSES:
        st.session_state.pop(f"ratio_{age_class.value}", None)


def _render_facility() -> None:
    """園設定（園名・開所・閉所・粒度・休業日）。"""
    st.markdown("#### 🏫 園設定")
    st.text_input(
        "園名",
        value=DEFAULT_SETTINGS.facility_name,
        key="facility_name",
    )
    left, right = st.columns(2)
    with left:
        day_open = st.time_input(
            "開所時刻",
            value=DEFAULT_SETTINGS.day_open,
            step=900,
            key="day_open",
        )
    with right:
        day_close = st.time_input(
            "閉所時刻",
            value=DEFAULT_SETTINGS.day_close,
            step=900,
            key="day_close",
        )
    granularity = st.select_slider(
        "時間帯の粒度",
        options=list(GRANULARITIES),
        value=DEFAULT_SETTINGS.granularity_min,
        format_func=lambda m: f"{m}分",
        key="granularity_min",
    )
    horizon = date.today() + timedelta(days=364)
    candidates = list(daterange(date.today(), horizon))
    default_closed = [d for d in candidates if d.weekday() == 6]
    closed = st.multiselect(
        "年間休業日（開所しない日）",
        options=candidates,
        default=default_closed,
        format_func=theme.format_day,
        key="closed_days",
        help="選択した日は必要人員もシフトも生成されません。",
    )
    labor_cost = st.number_input(
        "人件費目安（円／時間・パート係数）",
        min_value=500.0,
        max_value=10000.0,
        value=DEFAULT_SETTINGS.labor_cost_per_hour,
        step=100.0,
        key="labor_cost_per_hour",
    )
    state.set(
        state.KEY_SETTINGS,
        FacilitySettings(
            facility_name=st.session_state["facility_name"],
            day_open=day_open,
            day_close=day_close,
            granularity_min=int(granularity),
            closed_days=frozenset(closed),
            holiday_dates=frozenset(),
            labor_cost_per_hour=float(labor_cost),
        ),
    )
    state.reset(state.KEY_SLOTS)
    state.current_slots()
    # 以前は current_slots() が ValueError を握り潰していたため、
    # この except 節は到達不能で「開所・閉所時刻の設定が不正です」が
    # 結局一周もしていなかった。current_slots_error() で理由を読む。
    slots_error = state.current_slots_error()
    if slots_error:
        st.error(f"開所・閉所時刻の設定が不正です: {slots_error}")
    if not state.current_slots():
        st.warning(
            "時間帯が 0 個です。開所時刻 < 閉所時刻になっているか、"
            "時間帯の粒度（分）が 0 より大きいかを確認してください。"
        )


def _render_standard() -> None:
    """配置基準プリセットと自治体のローカルルールカード。"""
    st.markdown("#### ⚖️ 配置基準")
    presets: list[dict] = state.get(state.KEY_PRESETS) or local_rules.list_presets()
    current_key = state.get(state.KEY_STANDARD_KEY)
    keys = [p["key"] for p in presets]
    index = keys.index(current_key) if current_key in keys else 0
    record = st.selectbox(
        "自治体プリセット",
        options=presets,
        index=index,
        format_func=lambda r: f"{r['name']}（{r['summary']}）",
        key="standard_preset",
        help="所轄自治体の告示・条例などで基準が異なる場合は、所属する自治体のプリセットを選んでください。",
    )
    new_key = record["key"] if isinstance(record, dict) else str(record)
    if new_key != current_key:
        _reset_ratio_widgets()
        state.set(state.KEY_STANDARD_KEY, new_key)
        state.set(state.KEY_STANDARD, local_rules.get_standard(new_key))
        state.invalidate_pipeline()
    st.caption(f"出典: {record.get('source', '—')}")
    _render_overrides(local_rules.get_standard(state.get(state.KEY_STANDARD_KEY)))
    effective = state.current_standard()
    theme.rule_note_cards(
        local_rules.local_rule_notes(effective),
        title="この基準の適用範囲・根拠（自治体のローカルルール）",
    )


def _render_overrides(preset: Any) -> None:
    """園ごとの上乗せ設定（定員比・最低配置人数・延長緩和）。"""
    with st.expander("上乗せ設定（園ごとの差分）", expanded=False):
        st.caption("自治体の基準に、園の事情による差分を重ねます。数値を変更した項目だけが上乗せされます。")
        ratios: dict[AgeClass, float] = {}
        columns = st.columns(3)
        for position, age_class in enumerate(AGE_CLASSES):
            base = preset.ratios.get(age_class)
            default = float(base.children_per_staff) if base else 6.0
            with columns[position % 3]:
                ratios[age_class] = float(
                    st.number_input(
                        f"{age_class.value} 定員比（園児／職員）",
                        min_value=1.0,
                        max_value=40.0,
                        value=default,
                        step=0.5,
                        key=f"ratio_{age_class.value}",
                        help="例: 0歳児 3.0 なら 3 人に 1 人の保育士。",
                    )
                )
        left, right = st.columns(2)
        with left:
            min_staff = int(
                st.number_input(
                    "保育室の最低配置人数（2名ルール）",
                    min_value=1,
                    max_value=6,
                    value=int(preset.min_staff_per_room),
                    step=1,
                    key="min_staff_per_room",
                )
            )
        with right:
            relaxed = st.checkbox(
                "延長保育で支援員に代替させる",
                value=bool(preset.late_care_relaxed),
                key="late_care_relaxed",
                help="OFF にすると延長保育も保育士のみで満たす必要があります。",
            )
        enforce = st.checkbox(
            "2名ルールを必須要件として扱う",
            value=bool(state.get(state.KEY_ENFORCE_MIN_TWO)),
            key="ui_enforce_min_two",
            help="OFF にすると在園児がいる時間帯の底上げを行いません。",
        )
        overrides: dict[str, Any] = {
            "min_staff_per_room": min_staff,
            "late_care_relaxed": bool(relaxed),
            "ratios": ratios,
        }
        effective = local_rules.build_standard(
            state.get(state.KEY_STANDARD_KEY), overrides
        )
        state.set(state.KEY_STANDARD_OVERRIDES, overrides)
        state.set(state.KEY_STANDARD, effective)
        state.set(state.KEY_ENFORCE_MIN_TWO, bool(enforce))
        st.caption(
            f"適用中: {effective.name} ／ 休憩 {effective.break_minutes}分 ／ "
            f"保育標準時間 {effective.standard_time[0].strftime('%H:%M')}-"
            f"{effective.standard_time[1].strftime('%H:%M')}"
        )


def _render_period() -> None:
    """計画期間。開始日 widget からの返却値で ``days`` を再計算する。"""
    st.markdown("#### 📅 計画期間")
    left, right = st.columns(2)
    with left:
        start = st.date_input(
            "開始日",
            value=DEFAULT_RANGE_START,
            key="range_start",
            format="YYYY/MM/DD",
        )
    with right:
        count = int(
            st.number_input(
                "日数",
                min_value=1,
                max_value=31,
                value=DEFAULT_RANGE_DAYS,
                step=1,
                key="range_days",
            )
        )
    days = state.refresh_days(start, count)
    closed = state.get(state.KEY_SETTINGS).closed_days
    open_days = [d for d in days if d not in closed]
    st.caption(
        f"{len(days)} 日（{theme.format_day(days[0])} 〜 {theme.format_day(days[-1])}）"
        f"／ 開園 {len(open_days)} 日"
    )


def _render_weights() -> None:
    """目的関数の重みとソルバの時間制限。"""
    with st.expander("最適化オプション（目的関数の重み）", expanded=False):
        defaults = state.get(state.KEY_WEIGHTS)
        for name, label, low, high, step in state.WEIGHT_WIDGETS:
            st.slider(
                label,
                min_value=low,
                max_value=high,
                value=float(getattr(defaults, name)),
                step=step,
                key=f"weight_{name}",
            )
        st.caption(
            "配置不足のペナルティが大きいほど「基準を割らない」ことを優先します。"
            "過剰配置のペナルティが大きいほど「無駄な人件」を避けます。"
        )
        state.sync_weights()
        limit = int(
            st.slider(
                "PuLP の実行時間上限（秒）",
                min_value=10,
                max_value=300,
                value=int(state.get(state.KEY_TIME_LIMIT_SEC)),
                step=10,
                key="ui_time_limit_sec",
            )
        )
        state.set(state.KEY_TIME_LIMIT_SEC, limit)
        names = state.get(state.KEY_SOLVER_NAMES)
        st.caption(
            f"利用可能なソルバ: {', '.join(names) if names else 'なし（PuLP の CBC が使えません）'}"
        )


def _render_gas() -> None:
    """GAS 連携（設定されている場合のみ表示）。"""
    if not gas.available():
        st.caption("未設定（環境変数 SHIFTAI_GAS_URL で有効化）")
        return
    with st.expander("📡 Google スプレッドシート連携", expanded=False):
        config = gas.GasConfig.from_env()
        st.caption(
            f"接続先: {config.base_url if config else '—'} ／ シート: {config.sheet if config else '—'}"
        )
        if st.button("接続を確認（ping）", key="gas_ping", width="stretch"):
            _gas_call(lambda client: client.ping(), "疎通確認")
        if st.button("園児・職員データをシートへ送信", key="gas_push_tables", width="stretch"):
            _push_tables()


def _gas_call(action: Any, label: str) -> None:
    config = gas.GasConfig.from_env()
    if config is None:
        st.error("GAS の接続先が設定されていません（SHIFTAI_GAS_URL）。")
        return
    try:
        client = gas.GoogleAppsScriptClient(config)
        st.session_state[state.KEY_GAS_CLIENT] = client
        st.success(f"{label} 完了: {action(client)}")
    except gas.GasError as exc:
        st.error(f"{label} に失敗しました: {exc}")
    except gas.GasConfigError as exc:
        st.error(f"GAS の設定が不正です: {exc}")
    except Exception as exc:  # noqa: BLE001 - GAS 連携で画面を落とさない
        st.error(f"{label} に予期しないエラーが発生しました: {exc}")


def _push_tables() -> None:
    from shiftai import sample_data

    config = gas.GasConfig.from_env()
    if config is None:
        st.error("GAS の接続先が設定されていません（SHIFTAI_GAS_URL）。")
        return
    children = state.get(state.KEY_CHILDREN) or []
    staff = state.get(state.KEY_STAFF) or []
    preferences = state.get(state.KEY_PREFERENCES) or {}
    if not children or not staff:
        st.warning("先に園児・職員データを読み込んでください。")
        return
    days = state.current_days()
    frames = sample_data.sample_dataframes(
        days, children=children, staff=staff, preferences=preferences
    )
    try:
        client = gas.GoogleAppsScriptClient(config)
        st.session_state[state.KEY_GAS_CLIENT] = client
        results = client.sync_all({"children": frames["children"], "staff": frames["staff"]})
        st.success(f"送信しました: {', '.join(results)}")
    except gas.GasError as exc:
        st.error(f"送信に失敗しました: {exc}")
    except gas.GasConfigError as exc:
        st.error(f"GAS の設定が不正です: {exc}")
    except Exception as exc:  # noqa: BLE001 - GAS 連携で画面を落とさない
        st.error(f"送信に予期しないエラーが発生しました: {exc}")


def render() -> None:
    """サイドバー全体をレンダリングして session_state に反映する。"""
    with st.sidebar:
        st.markdown(f"## {APP_ICON} 設定")
        _render_facility()
        st.divider()
        _render_standard()
        st.divider()
        _render_period()
        st.divider()
        _render_weights()
        st.divider()
        _render_gas()
        st.divider()
        theme.legend(
            [
                (f"勤務 ({COLOR_WORK})", COLOR_WORK),
                (f"休憩 ({COLOR_BREAK})", COLOR_BREAK),
                (f"オフ ({COLOR_OFF})", "#BDBDBD"),
            ]
        )
        st.caption(
            f"労働基準法の週法定労働時間は {STATUTORY_WEEKLY_WORK_HOURS} 時間。"
            "本アプリの最適解は法令・基準への適合を優先しますが、"
            "最終判断は園長・設置責任者の確認を推奨します。"
        )
