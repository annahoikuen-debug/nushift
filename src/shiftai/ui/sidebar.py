"""サイドバー（園設定・配置基準プリセット・計画期間・最適化オプション・GAS連携）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import streamlit as st

from shiftai import gas_client as gas
from shiftai import local_rules
from shiftai.config import (
    APP_ICON,
    APP_TIME_INPUT_STEP_SECONDS,
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
from shiftai.domain import (
    HEADCOUNT_FACILITY_FORMULA,
    AgeClass,
    FacilitySettings,
    daterange,
    to_minutes,
    to_time,
)
from shiftai.shift_patterns import (
    DEFAULT_PATTERNS,
    ShiftPattern,
    default_patterns,
)
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


def _invalidate_if_changed(previous: Any, current: Any, reason: str) -> None:
    """入力が実際に変わっていないなら何もしない。変わっていれば無効化する。

    Streamlit は毎フレームサイドバーを描き直すため、値が変わっていないのに
    毎回最適化結果を破棄すると結果が消えてしまう。だから実際の差分を見る。

    **初回描画（指紋が空）は無効化しない。** それなら読み込み直後の
    計算結果まで消えてしまう。
    """
    if not state.get(state.KEY_INPUT_FINGERPRINT):
        return
    if previous == current:
        return
    state.mark_inputs_changed(reason)


def _render_facility(*, times: bool = True) -> None:
    """園設定（園名・開所・閉所・粒度・休業日）。

    ``times=False`` は **ウィザードが同じキーのウィジェットを
    別に描画している**場合の描画。开所・閉所時刻のウィジェットは作らず、
    ウィザードが書き込んだ値を読み取るだけにする。同じキーを 2 つの
    ウィジェットで使わないようにするため（Streamlit は DuplicateWidgetID で
    落ちる）、どちらかが所有する。
    """
    st.markdown("#### 🏫 園設定")
    st.text_input(
        "園名",
        value=DEFAULT_SETTINGS.facility_name,
        key="facility_name",
    )
    if times:
        left, right = st.columns(2)
        with left:
            day_open = st.time_input(
                "開所時刻",
                value=DEFAULT_SETTINGS.day_open,
                step=APP_TIME_INPUT_STEP_SECONDS,
                key="day_open",
            )
        with right:
            day_close = st.time_input(
                "閉所時刻",
                value=DEFAULT_SETTINGS.day_close,
                step=APP_TIME_INPUT_STEP_SECONDS,
                key="day_close",
            )
    else:
        day_open = st.session_state.get("day_open", DEFAULT_SETTINGS.day_open)
        day_close = st.session_state.get("day_close", DEFAULT_SETTINGS.day_close)
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
    holidays = st.multiselect(
        "祝日・行事日（開所するが基準を適用しない日）",
        options=candidates,
        default=[],
        format_func=theme.format_day,
        key="holiday_dates",
        help=(
            "該当日は必要人員の基準が適用外（is_binding=False）になり、"
            "休日勤務不可の職員は出勤できません。"
            "年間休業日と併記した場合は年間休業日が優先されます。"
            "CLI の --holiday に相当します。"
        ),
    )
    labor_cost = st.number_input(
        "人件費目安（円／時間・パート係数）",
        min_value=500.0,
        max_value=10000.0,
        value=DEFAULT_SETTINGS.labor_cost_per_hour,
        step=100.0,
        key="labor_cost_per_hour",
    )
    previous = state.get(state.KEY_SETTINGS)
    state.set(
        state.KEY_SETTINGS,
        FacilitySettings(
            facility_name=st.session_state["facility_name"],
            day_open=day_open,
            day_close=day_close,
            granularity_min=int(granularity),
            closed_days=frozenset(closed),
            holiday_dates=frozenset(holidays),
            labor_cost_per_hour=float(labor_cost),
        ),
    )
    state.reset(state.KEY_SLOTS)
    state.current_slots()
    _invalidate_if_changed(previous, state.get(state.KEY_SETTINGS), "園設定を変更しました")
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
        state.mark_inputs_changed("自治体プリセットを変更しました")
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
        st.caption(
            "自治体の基準に、園の事情による差分を重ねます。数値を変更した項目だけが上乗せされます。"
        )
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
        effective = local_rules.build_standard(state.get(state.KEY_STANDARD_KEY), overrides)
        previous_effective = state.get(state.KEY_STANDARD)
        state.set(state.KEY_STANDARD_OVERRIDES, overrides)
        state.set(state.KEY_STANDARD, effective)
        state.set(state.KEY_ENFORCE_MIN_TWO, bool(enforce))
        _invalidate_if_changed(
            previous_effective,
            effective,
            "配置基準の上乗せを変更しました",
        )
        st.caption(
            f"適用中: {effective.name} ／ 休憩 {effective.break_minutes}分 ／ "
            f"保育標準時間 {effective.standard_time[0].strftime('%H:%M')}-"
            f"{effective.standard_time[1].strftime('%H:%M')}"
        )
        if effective.headcount_mode == HEADCOUNT_FACILITY_FORMULA:
            st.caption(
                f"⚠️ 認可外保育施設の算手法: {local_rules.headcount_mode_label(effective)}"
                f"（合計＋{effective.headcount_extra} 名）。"
                "保育標準時間の制度ではなく、"
                "**主たる開所時間 11 時間**として扱っている。",
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
    previous_days = list(state.get(state.KEY_DAYS) or [])
    days = state.refresh_days(start, count)
    closed = state.get(state.KEY_SETTINGS).closed_days
    open_days = [d for d in days if d not in closed]
    st.caption(
        f"{len(days)} 日（{theme.format_day(days[0])} 〜 {theme.format_day(days[-1])}）"
        f"／ 開園 {len(open_days)} 日"
    )
    if days != previous_days and state.get(state.KEY_INPUT_FINGERPRINT):
        state.mark_inputs_changed("計画期間を変更しました")


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
        if all(
            float(getattr(state.get(state.KEY_WEIGHTS), name, 0.0)) > 0
            for name in state.FAIRNESS_WEIGHT_NAMES
        ):
            st.caption(
                "公平性のペナルティは「職員ごとの早番・遅番・土曜出勤の回数の最大と最小の差」"
                "を縮める方向に働きます。0 のまま無効にしておけば変数は作られません。"
            )
        else:
            st.caption(
                "早番・遅番・土曜出勤の公平配分は既定では無効です。"
                "気になる場合は上のスライダーを上げます（0 なら変数は作られません）。"
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


def _render_patterns() -> None:
    """勤務パターン（早番・日勤・遅番）の定義（優先2）。

    有効にすると、勤務ブロックの開始・終了時刻を aquí の境界へ引き寄せる。
    """
    st.markdown("#### ⏰ 勤務パターン（早番・日勤・遅番）")
    settings = state.current_settings()
    if not state.get(state.KEY_PATTERNS_ENABLED):
        st.checkbox(
            "勤務パターンを有効にする",
            value=False,
            key=state.KEY_PATTERNS_ENABLED,
            help=(
                "MILP に「勤務ブロックの境界をパターンへ引き寄せる」目的関数を"
                "追加し、生成後に境界の整列も行います。ハード制約ではないので、"
                "揃う職員がいても解が消えることはありません。"
            ),
        )
    if not state.get(state.KEY_PATTERNS_ENABLED):
        return

    presets = _pattern_presets(settings)
    preset_keys = list(presets)
    chosen = st.selectbox(
        "プリセット",
        options=preset_keys,
        index=0,
        key="pattern_preset",
        help="選ぶと下の一覧をその内容で上書きします。個別に編集もできます。",
    )
    if st.button("プリセットを適用", key="pattern_apply_preset"):
        state.set_patterns(presets[chosen])
        st.rerun()

    current = state.current_patterns()
    defaults = presets[chosen] if chosen in presets else presets[preset_keys[0]]
    if not current:
        state.set_patterns(defaults)
        current = defaults

    rows: list[ShiftPattern] = []
    for i, base in enumerate(defaults):
        label = st.text_input(f"枠{i + 1} の名前", value=base.label, key=f"pattern_name_{i}")
        cols = st.columns(2)
        with cols[0]:
            start = st.time_input(
                f"枠{i + 1} の開始",
                value=base.start,
                step=APP_TIME_INPUT_STEP_SECONDS,
                key=f"pattern_start_{i}",
            )
        with cols[1]:
            end = st.time_input(
                f"枠{i + 1} の終了",
                value=base.end,
                step=APP_TIME_INPUT_STEP_SECONDS,
                key=f"pattern_end_{i}",
            )
        if end > start:
            rows.append(
                ShiftPattern(key=f"p{i}", label=label or f"枠{i + 1}", start=start, end=end)
            )
        else:
            st.warning(f"枠{i + 1} は終了が始業と同じか後のため無視されます。")

    if st.button("追加", key="pattern_add", help="新しい枠を末尾に追加します。"):
        open_min = to_minutes(settings.day_open)
        last_end = max((to_minutes(p.end) for p in rows), default=open_min)
        base_start = min(
            max(last_end - 540, to_minutes(settings.day_close) - 540),
            to_minutes(settings.day_close) - 60,
        )
        base_start = max(base_start, to_minutes(settings.day_open))
        new_end = min(base_start + 540, to_minutes(settings.day_close))
        rows.append(
            ShiftPattern(
                key=f"p{len(rows)}",
                label=f"枠{len(rows) + 1}",
                start=to_time(base_start),
                end=to_time(new_end),
            )
        )
        state.set_patterns(rows)
        st.rerun()

    state.set_patterns(rows)
    if rows:
        st.caption(" / ".join(f"{p.label} {p.span()}" for p in rows))
        st.checkbox(
            "生成後に境界を丸める（スナップ）",
            value=bool(state.get(state.KEY_PATTERN_SNAP)),
            key=state.KEY_PATTERN_SNAP,
            help=(
                "ソルバのペナルティで揃わなかったブロックを、配置基準を破らない範囲で"
                "パターン境界へ移動します。"
            ),
        )
        if st.button("パターンを削除", key="pattern_clear"):
            state.set_patterns(())
            st.rerun()


def _pattern_presets(settings: FacilitySettings) -> dict[str, tuple[ShiftPattern, ...]]:
    """勤務パターンのプリセット集。"""
    return {
        "園の開所・閉所から自動生成（9時間）": default_patterns(
            settings.day_open, settings.day_close, shift_hours=9.0
        ),
        "標準（7:30-16:30 / 8:30-17:30 / 10:30-19:30）": DEFAULT_PATTERNS,
        "8時間枠（早番・日勤・遅番）": default_patterns(
            settings.day_open, settings.day_close, shift_hours=8.0
        ),
        "10時間枠（早番・日勤・遅番）": default_patterns(
            settings.day_open, settings.day_close, shift_hours=10.0
        ),
    }


def render(*, simple: bool = False, period: bool = True, times: bool = True) -> None:
    """サイドバー全体をレンダリングして session_state に反映する。

    ``simple=True``（既定のシンプルモード）のときは、はじめに必要な
    「園設定」と「計画期間」だけを目立つ位置に出し、残りの詳細設定
    （配置基準プリセット・最適化オプション・GAS 連携）は閉じた
    expander の中に畳む。**expander は閉じていても中身は描画される**ため、
    上級者モードとの切り替えで設定が消えることはない。

    ``period=False`` / ``times=False`` は、入力をウィザードへ移すために使う。
    ``wizard.render`` が同じキー（``range_start`` / ``day_open`` など）の
    ウィジェットを描くため、こちらは描画しない。**両方とも既定 True の
    ままで呼ぶと DuplicateWidgetID で落ちる**ので、呼び出し側が必ず選ぶ。
    """
    with st.sidebar:
        st.markdown(f"## {APP_ICON} 設定")
        _render_facility(times=times)
        st.divider()
        if period:
            _render_period()
        else:
            st.markdown("#### 📅 計画期間")
            st.caption("計画期間はウィザードの「🏫 園の条件」で設定します。")
        if simple:
            with st.expander("⚙️ 詳細設定（上級者向け）", expanded=False):
                theme.tip(
                    "❓ 何を変更できるの？",
                    "<p>所轄自治体の配置基準・勤務パターン（早番／日勤／遅番）"
                    "・目的関数の重み・シート連携など。</p>"
                    "<p>通常は変更する必要はありません。園ごとの事情がある場合に"
                    "使ってください。</p>",
                )
                _render_standard()
                st.divider()
                _render_patterns()
                st.divider()
                _render_weights()
                st.divider()
                _render_gas()
        else:
            st.divider()
            _render_standard()
            st.divider()
            _render_patterns()
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
        theme.tip(
            "ℹ️ 労働基準の取り扱いについて",
            f"<p>本アプリが使う週の内部目安は <b>{STATUTORY_WEEKLY_WORK_HOURS} 時間</b>です。</p>"
            "<p>労働基準法の枠組みは月45時間・年360時間で、旧来の週44時間は"
            "2019年の改正で法定の上限ではなくなっています。厳しい方に"
            "合わせているため法令違反は生じません。</p>"
            "<p>最適解は法令・基準への適合を優先しますが、"
            "最終判断は園長・設置責任者の確認を推奨します。</p>",
        )
