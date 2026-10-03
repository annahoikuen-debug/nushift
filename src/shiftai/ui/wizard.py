"""初心者向け入力ウィザード（最初はこの画面が開く）。

3 つの表（園児・職員・希望休）を一度に見せる代わりに、**1 つずつ**
「何を入力するか」「今何が起きているか」を順に見せる。はじめて使う園長・
主任でも、列の定義を調べずにシフト作成までたどり着けることを目指す。

設計方針:

* **情報は隠さない、消さない。** 従来は常に目に入っていたグレーの説明は
  ``theme.tip`` のツールチップへ移す（``tab_data`` と同じ流儀）。
* **ウィザードは描画の入れ替えにすぎない。** データの保存先は
  ``frame_children`` / ``frame_staff`` / ``frame_preferences`` で、
  「まとめて入力」モードと共有する。どちらのモードでも最後の読み込みは
  ``tab_data.render_apply_block`` を通る。
* **同じキーのウィジェットを二重描画しない。** 開所・閉所時刻と計画期間は
  ウィザードがサイドバーと同じキー（``day_open`` など）で描くため、
  ``sidebar.render`` には ``period=False, times=False`` を渡して
  そちらを隠す。逆も同様。
"""

from __future__ import annotations

from datetime import date, time

import pandas as pd
import streamlit as st

from shiftai import sample_data
from shiftai.config import (
    APP_TIME_INPUT_STEP_SECONDS,
    DEFAULT_RANGE_DAYS,
    DEFAULT_RANGE_START,
)
from shiftai.domain import FacilitySettings
from shiftai.ui import state, tab_data, theme

KEY_MODE = "wizard_mode"
"""入力方法（``MODE_WIZARD`` / ``MODE_BULK``）。ウィジェットキーとして使う。"""

KEY_ANSWERED = "wizard_answered"
"""最初の「ウィザードで入力しますか？」に答えたか（セッション内）。"""

KEY_STEP = "wizard_step"

MODE_WIZARD = "wizard"
MODE_BULK = "bulk"
MODE_OPTIONS: tuple[str, ...] = (MODE_WIZARD, MODE_BULK)
DEFAULT_MODE = MODE_WIZARD
"""未回答のあいだの既定。はじめての方はウィザードからはじめるのが早い。"""

MODE_LABELS: dict[str, str] = {
    MODE_WIZARD: "🧙 ウィザードで順に入力する（おすすめ）",
    MODE_BULK: "📋 まとめて入力する（貼り付け・上級者向け）",
}

STEP_LABELS: tuple[str, ...] = (
    "1 園の条件",
    "2 園児",
    "3 職員",
    "4 希望休",
    "5 確認",
)
STEP_HINTS: tuple[str, ...] = (
    "開所時刻と計画期間を決めます。",
    "その日に在園する園児を 1 行ずつ登録します。",
    "勤務する職員を 1 人ずつ登録します。",
    "出勤できない日を登録します（空でもかまいません）。",
    "内容を確認して読み込みます。",
)
LAST_STEP = len(STEP_LABELS) - 1

SOURCES: tuple[str, ...] = (
    "📄 ファイルを選ぶ",
    "🎲 サンプルを使う",
    "✏️ 直接入力する",
)

INTRO_BODY = (
    "<p><b>ウィザードで順に入力する</b>（おすすめ）</p>"
    "<p>5 つのステップで、園の条件 → 園児 → 職員 → 希望休 → 確認 の順に"
    "入力します。各ステップで「何をすればよいか」が表示されます。</p>"
    "<p><b>まとめて入力する</b></p>"
    "<p>園児・職員・希望休の 3 表をまとめて貼り付け・編集します。"
    "園の基幹システムから出力した CSV をそのまま取り込みたい場合はこちら。</p>"
    "<p>どちらもあとからサイドバーの「入力方法」で切り替えられます。"
    "入力済みの内容は保持されます。</p>"
)


# ---------------------------------------------------------------------------
# 入力方法（ウィザード／まとめて入力）
# ---------------------------------------------------------------------------


def answered() -> bool:
    """最初の質問に答えたか。まだなら質問を出す。"""
    return bool(st.session_state.get(KEY_ANSWERED, False))


def mode() -> str:
    """現在の入力方法を返す（未設定なら既定）。"""
    value = st.session_state.get(KEY_MODE, None)
    return value if value in MODE_OPTIONS else DEFAULT_MODE


def use_wizard() -> bool:
    return mode() == MODE_WIZARD


def mode_selector(label: str = "入力方法") -> str:
    """入力方法のラジオ（問い合わせ画面とサイドバーが同じキーで共有する）。

    キーが同じでパラメータも同じなので、問い合わせ画面（メインarea）から
    サイドバーへ移動しても値は保持される。**両方同時には描かない**
    （同じキーのウィジェットを 2 つ作ると Streamlit が例外を投げる）。
    """
    options = list(MODE_OPTIONS)
    return st.radio(
        label,
        options,
        index=options.index(mode()),
        format_func=lambda value: MODE_LABELS[value],
        key=KEY_MODE,
        help="あとからどちらでも切り替えられます。",
    )


def render_intro() -> None:
    """最初だけ出る「ウィザードで入力しますか？」の質問。"""
    st.markdown("### 🧙 はじめまして。入力方法を選んでください")
    st.caption(
        "園児の登降園予定・職員・希望休の 3 つを入力します。"
        "はじめての方はウィザードを選ぶと、1 画面 1 作業の進め方になります。"
    )
    mode_selector()
    if st.button(
        "▶ この方法で入力する",
        key="wizard_start",
        type="primary",
        width="stretch",
        help="選んだ方法でデータ投入画面を開きます。",
    ):
        st.session_state[KEY_ANSWERED] = True
        st.rerun()
    theme.tip("❓ どちらを選べばよいですか？", INTRO_BODY)


def current_step() -> int:
    value = st.session_state.get(KEY_STEP, 0)
    try:
        step = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(step, LAST_STEP))


def set_step(step: int) -> None:
    st.session_state[KEY_STEP] = max(0, min(int(step), LAST_STEP))


# ---------------------------------------------------------------------------
# 園の条件（開所時刻・計画期間）
# ---------------------------------------------------------------------------


def render_conditions(*, expanded: bool = False) -> None:
    """開所・閉所時刻と計画期間。

    **毎 run 描画する。** Streamlit は描画されなかったウィジェットの
    ``session_state`` を掃除するため、ステップ 2 以降で条件ウィジェットを
    描画しないと計画期間が既定値へ戻ってしまう。ステップ 1 以外は折りたたんで
    出す（``st.expander`` は閉じていても中身は毎 run 描画される）。
    """
    settings = state.current_settings()
    with st.expander("🏫 園の条件（開所時刻・計画期間）", expanded=expanded):
        st.caption(
            "何時に開き、何時まで開いているか・何日間のシフトを作るかを決めます。"
            "「まとめて入力」モードのサイドバーと同じ項目です。"
        )
        left, right = st.columns(2)
        with left:
            day_open = st.time_input(
                "開所時刻",
                value=settings.day_open,
                step=APP_TIME_INPUT_STEP_SECONDS,
                key="day_open",
            )
        with right:
            day_close = st.time_input(
                "閉所時刻",
                value=settings.day_close,
                step=APP_TIME_INPUT_STEP_SECONDS,
                key="day_close",
            )
        third, fourth = st.columns(2)
        with third:
            start = st.date_input(
                "計画期間の開始日",
                value=DEFAULT_RANGE_START,
                key="range_start",
                format="YYYY/MM/DD",
            )
        with fourth:
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
        _apply_conditions(day_open, day_close, start, count)


def _apply_conditions(day_open: time, day_close: time, start: date, count: int) -> None:
    """ウィジェット値を園設定と計画期間へ反映する（サイドバーと同じ処理）。"""
    settings = state.current_settings()
    state.set(
        state.KEY_SETTINGS,
        FacilitySettings(
            facility_name=settings.facility_name,
            day_open=day_open,
            day_close=day_close,
            granularity_min=settings.granularity_min,
            closed_days=settings.closed_days,
            holiday_dates=settings.holiday_dates,
            rooms=settings.rooms,
            labor_cost_per_hour=settings.labor_cost_per_hour,
        ),
    )
    state.reset(state.KEY_SLOTS)
    slots_error = state.current_slots_error()
    if slots_error:
        st.error(f"開所・閉所時刻の設定が不正です: {slots_error}")
    elif not state.current_slots():
        st.warning("時間帯が 0 個です。開所時刻 < 閉所時刻になっているか確認してください。")
    days = state.refresh_days(start, count)
    open_days = [d for d in days if d not in settings.closed_days]
    st.caption(
        f"{len(days)} 日（{theme.format_day(days[0])} 〜 {theme.format_day(days[-1])}）"
        f"／ 開園 {len(open_days)} 日"
    )


# ---------------------------------------------------------------------------
# 表の入力ステップ
# ---------------------------------------------------------------------------


def _source_of(kind: str) -> str:
    value = st.session_state.get(f"wizard_source_{kind}", SOURCES[0])
    return value if value in SOURCES else SOURCES[0]


def _load_sample_step(kind: str, seed: int) -> None:
    title = tab_data.table_title(kind)
    st.caption(
        f"{title} のサンプルデータ（園児 {sample_data.TOTAL_CHILDREN} 名 / "
        f"職員 {sample_data.TOTAL_STAFF} 名）を読み込みます。"
        "あとから実際のデータに差し替えても構いません。"
    )
    if st.button(
        "🎲 サンプルデータを読み込む",
        key=f"wizard_sample_{kind}",
        type="primary" if kind == "staff" else "secondary",
        width="stretch",
    ):
        frame = tab_data.load_sample(kind, state.current_days(), seed)
        tab_data.store_frame(kind, frame)
        st.toast(f"{title} のサンプルを {len(frame)} 行読み込みました", icon="✅")


def render_table_step(kind: str, *, seed: int = 42, required: bool = True) -> None:
    """園児・職員・希望休の入力ステップ。"""
    title = tab_data.table_title(kind)
    label, detail = tab_data.table_tip_body(kind)
    tab_data.table_tip(kind, label, detail)

    source = st.radio(
        "この表の入力方法",
        SOURCES,
        index=list(SOURCES).index(_source_of(kind)),
        key=f"wizard_source_{kind}",
        horizontal=True,
    )
    if source == SOURCES[0]:
        uploads = st.file_uploader(
            f"{title} のファイル（CSV / Excel）",
            type=["csv", "xlsx"],
            accept_multiple_files=True,
            key=f"upload_{kind}",
            help="1 つ以上のファイルを選べます。列名は自動で読み替えます。",
        )
        if uploads:
            try:
                merged = pd.concat(tab_data.read_uploads(kind, uploads), ignore_index=True)
                tab_data.store_frame(kind, merged)
                st.success(f"{len(merged)} 行を読み込みました。")
            except Exception as exc:  # noqa: BLE001 - 取り込み失敗で画面を落とさない
                st.error(f"ファイルを読み込めませんでした: {exc}")
        if kind == "children":
            # 園の基幹システム（CoDMON / キッズリーなど）由来の CSV は、
            # 専用の取り込み口を通したほうが短時間保育の扱いも正しい。
            tab_data.render_importer()
    elif source == SOURCES[1]:
        _load_sample_step(kind, seed)
    else:
        st.caption(
            "下の表に 1 行ずつ入力します。左下の「＋」で行を追加し、"
            "不要な行は「−」で消してください。"
        )

    edited = st.data_editor(
        tab_data.editor_frame(kind),
        num_rows="dynamic",
        hide_index=True,
        column_config=tab_data.column_config(kind),
        key=f"editor_{kind}",
        height=300,
        width="stretch",
    )
    tab_data.store_frame(kind, edited, reset_editor=False)
    rows = tab_data.frame_rows(kind)
    st.caption(f"{title}: {rows} 行")
    tab_data.render_table_validation(kind)
    if required and rows == 0:
        st.warning(
            f"{title} が空です。サンプルを読み込むか、ファイルを選ぶか、下の表に入力してください。"
        )


# ---------------------------------------------------------------------------
# 確認ステップ
# ---------------------------------------------------------------------------


def render_review() -> None:
    """入力内容の確認と、読み込みの実行。"""
    st.markdown("#### ステップ 5: 確認")
    left, right, third = st.columns(3)
    with left:
        st.metric("園児", f"{tab_data.frame_rows('children')} 行")
    with right:
        st.metric("職員", f"{tab_data.frame_rows('staff')} 行")
    with third:
        st.metric("希望休", f"{tab_data.frame_rows('preferences')} 行")

    days = state.current_days()
    if days:
        st.caption(
            f"計画期間: {theme.format_day(days[0])} 〜 {theme.format_day(days[-1])}"
            f"（{len(days)} 日）"
        )
    tab_data.render_apply_block()
    if state.get(state.KEY_LOAD_RESULT) is not None:
        theme.next_step_hint(0)


# ---------------------------------------------------------------------------
# ステップ送受信
# ---------------------------------------------------------------------------


def _gate(step: int) -> str:
    """次のステップへ進めない理由（空文字なら進める）。"""
    if step == 1 and tab_data.frame_rows("children") == 0:
        return "先に園児（ステップ 2）を 1 行以上入力してください。"
    if step == 2 and tab_data.frame_rows("staff") == 0:
        return "先に職員（ステップ 3）を 1 行以上入力してください。"
    return ""


def _render_nav() -> None:
    step = current_step()
    blocked = _gate(step)
    left, right, tail = st.columns([1, 1, 3])
    with left:
        if st.button("◀ 戻る", key="wizard_prev", width="stretch", disabled=step == 0):
            set_step(step - 1)
            st.rerun()
    with right:
        if st.button(
            "次へ ▶",
            key="wizard_next",
            width="stretch",
            type="primary",
            disabled=step == LAST_STEP or bool(blocked),
            help=blocked or "次のステップに進みます。",
        ):
            set_step(step + 1)
            st.rerun()
    with tail:
        st.caption(f"ステップ {step + 1} / {len(STEP_LABELS)}")


# ---------------------------------------------------------------------------
# ウィザード本体
# ---------------------------------------------------------------------------


def render(seed: int = 42) -> None:
    """ウィザード画面（タブ1 の本体）。"""
    theme.step_indicator(0)
    theme.heading(0)
    step = current_step()
    render_conditions(expanded=step == 0)
    st.divider()
    theme.chips(STEP_LABELS, step, mark_done=True)
    st.caption(STEP_HINTS[step])

    if step == 0:
        st.markdown("#### ステップ 1: 園の条件")
        st.info(
            "上の「🏫 園の条件」を開いて、開所時刻と計画期間を入力してください。",
            icon="👇",
        )
    elif step == 1:
        st.markdown("#### ステップ 2: 園児（登降園予定）")
        render_table_step("children", seed=seed)
    elif step == 2:
        st.markdown("#### ステップ 3: 職員")
        render_table_step("staff", seed=seed)
    elif step == 3:
        st.markdown("#### ステップ 4: 希望休")
        render_table_step("preferences", seed=seed, required=False)
    else:
        render_review()

    _render_nav()
    theme.caveat_box()


def render_sidebar_control() -> None:
    """サイドバーの入力方法切り替え（質問に答えた後にだけ描く）。"""
    if not answered():
        return
    mode_selector("入力方法")
