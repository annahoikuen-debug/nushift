"""ページ設定・CSS・共通ウィジェット。

``apply_page_config`` はエントリポイントから最初に呼ばれる必要がある。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from html import escape
from typing import Any

import streamlit as st

from shiftai.config import (
    APP_ICON,
    APP_TITLE,
    APP_VERSION,
    COLOR_ACCENT,
    COLOR_BREAK,
    COLOR_BREAK_INK,
    COLOR_FIXED,
    COLOR_INK,
    COLOR_OFF,
    COLOR_OVER,
    COLOR_OVER_INK,
    COLOR_SHORTFALL,
    COLOR_SHORTFALL_INK,
    COLOR_WORK,
    COLOR_WORK_INK,
)
from shiftai.domain import format_jp_date
from shiftai.ui.steps import (
    EXPERT_STEPS,
    SIMPLE_INDEX,
    SIMPLE_STEPS,
    Step,
    steps_for_mode,
)

PAGE_TITLE = APP_TITLE
PAGE_ICON = APP_ICON
LAYOUT = "wide"

CAVEAT_TEXT = (
    "⚠️ **この数値は要確認**です。自治体の運用基準・園条例・県知事の告示は"
    "施設ごとに異なるため、ここに表示している定員比・時間帯区分は"
    "公開情報から起こした参考値です。実際の運用前に、"
    "所轄の市町村の保育課（自治体の保育主管課）へ個別に確認してください。"
)

CSS = f"""
<style>
:root {{
  --shiftai-work: {COLOR_WORK};
  --shiftai-break: {COLOR_BREAK};
  --shiftai-off: {COLOR_OFF};
  --shiftai-short: {COLOR_SHORTFALL};
  --shiftai-over: {COLOR_OVER};
}}
html, body, [class*="css"] {{
  font-family: "Hiragino Kaku Gothic ProN", "Noto Sans JP", "Yu Gothic UI",
               Meiryo, system-ui, sans-serif;
}}
.block-container {{
  padding-top: 2.2rem;
  padding-bottom: 4rem;
  max-width: 1800px;
}}
.shiftai-ver {{
  color: rgba(49, 51, 63, 0.45);
  font-size: 0.72rem;
  text-align: right;
  margin-top: -0.4rem;
}}
.shiftai-metric {{
  border: 1px solid rgba(49, 51, 63, 0.12);
  border-left: 5px solid var(--shiftai-work);
  border-radius: 0.6rem;
  padding: 0.55rem 0.85rem 0.7rem 0.85rem;
  background: #ffffff;
  min-height: 5.4rem;
}}
.shiftai-metric.warn {{ border-left-color: var(--shiftai-short); }}
.shiftai-metric.info {{ border-left-color: var(--shiftai-over); }}
.shiftai-metric.neutral {{ border-left-color: #9e9e9e; }}
.shiftai-metric-label {{
  font-size: 0.78rem;
  color: rgba(49, 51, 63, 0.7);
  margin-bottom: 0.15rem;
}}
.shiftai-metric-value {{
  font-size: 1.5rem;
  font-weight: 700;
  line-height: 1.15;
  color: {COLOR_INK};
}}
.shiftai-metric-note {{
  font-size: 0.72rem;
  color: rgba(49, 51, 63, 0.6);
  margin-top: 0.2rem;
}}
.shiftai-legend {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem 1.1rem;
  font-size: 0.8rem;
  color: rgba(49, 51, 63, 0.78);
  margin: 0.2rem 0 0.6rem 0;
}}
.shiftai-chip {{
  border-radius: 0.35rem;
  padding: 0.1rem 0.5rem;
  font-size: 0.78rem;
  border: 1px solid rgba(49, 51, 63, 0.18);
  white-space: nowrap;
}}
.shiftai-note-card {{
  border: 1px solid rgba(49, 51, 63, 0.12);
  border-left: 4px solid var(--shiftai-over);
  border-radius: 0.5rem;
  padding: 0.6rem 0.9rem 0.7rem 0.9rem;
  margin: 0.45rem 0 0.7rem 0;
  background: #fbfcfe;
}}
.shiftai-note-card h5 {{
  margin: 0 0 0.3rem 0;
  font-size: 0.95rem;
  color: {COLOR_INK};
}}
.shiftai-note-card p {{
  margin: 0 0 0.25rem 0;
  font-size: 0.85rem;
  line-height: 1.6;
  white-space: pre-wrap;
}}
.shiftai-quote {{
  border-left: 3px solid #b0bec5;
  margin: 0.35rem 0 0.1rem 0;
  padding: 0.15rem 0 0.15rem 0.7rem;
  color: rgba(49, 51, 63, 0.72);
  font-size: 0.8rem;
  line-height: 1.6;
  white-space: pre-wrap;
}}
.shiftai-caveat {{
  border: 1px solid #ffb74d;
  border-radius: 0.5rem;
  background: #fff8e1;
  padding: 0.55rem 0.85rem;
  font-size: 0.85rem;
  line-height: 1.65;
  margin: 0.35rem 0 0.6rem 0;
}}
.shiftai-shortfall {{ background-color: {COLOR_SHORTFALL}22; color: {COLOR_SHORTFALL_INK}; font-weight: 600; }}
.shiftai-overstaff {{ background-color: {COLOR_OVER}1a; color: {COLOR_OVER_INK}; }}
.shiftai-ok {{ background-color: {COLOR_WORK}1a; color: {COLOR_WORK_INK}; }}
.shiftai-fixed {{ box-shadow: inset 0 0 0 2px {COLOR_FIXED}; }}
.shiftai-table-tight td, .shiftai-table-tight th {{
  font-size: 0.8rem;
  padding: 0.22rem 0.4rem;
}}
/* --- 日別ガントチャート --- */
.shiftai-gantt {{
  --gantt-name-col: 9.5rem;
  --gantt-hours-col: 3.4rem;
  --gantt-min-track: 26rem;
  border: 1px solid rgba(49, 51, 63, 0.12);
  border-radius: 0.5rem;
  background: #ffffff;
  padding: 0.5rem 0.7rem 0.55rem 0.7rem;
}}
.shiftai-gantt-scroll {{
  max-height: 32rem;
  overflow: auto;
}}
.shiftai-gantt-head, .shiftai-gantt-row {{
  display: grid;
  grid-template-columns: var(--gantt-name-col)
                        minmax(var(--gantt-min-track), 1fr)
                        var(--gantt-hours-col);
  align-items: center;
  column-gap: 0.45rem;
}}
.shiftai-gantt-head {{
  position: sticky;
  top: 0;
  z-index: 2;
  background: #ffffff;
  padding-bottom: 0.22rem;
  border-bottom: 1px solid rgba(49, 51, 63, 0.14);
  font-size: 0.7rem;
  color: rgba(49, 51, 63, 0.6);
}}
.shiftai-gantt-row {{ padding: 0.16rem 0; }}
.shiftai-gantt-row + .shiftai-gantt-row {{
  border-top: 1px dashed rgba(49, 51, 63, 0.08);
}}
.shiftai-gantt-name {{
  font-size: 0.78rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.shiftai-gantt-id {{
  font-size: 0.7rem;
  color: rgba(49, 51, 63, 0.5);
  margin-right: 0.3rem;
}}
.shiftai-gantt-axis {{ position: relative; height: 0.95rem; }}
.shiftai-gantt-tick {{
  position: absolute;
  top: 0;
  font-size: 0.66rem;
  color: rgba(49, 51, 63, 0.6);
  white-space: nowrap;
}}
.shiftai-gantt-track {{
  position: relative;
  height: 1.1rem;
  border-radius: 0.25rem;
  background-color: rgba(49, 51, 63, 0.04);
}}
.shiftai-gantt-bar {{
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  display: flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  height: 0.9rem;
  padding: 0 0.15rem;
  border-radius: 0.2rem;
  font-size: 0.65rem;
  line-height: 1;
  white-space: nowrap;
  overflow: hidden;
}}
.shiftai-gantt-bar--work {{
  background-color: var(--shiftai-work);
  color: #ffffff;
}}
.shiftai-gantt-bar--break {{
  background-color: #ffffff;
  background-image: repeating-linear-gradient(
    45deg, var(--shiftai-break) 0 3px, #ffffff 3px 6px
  );
  border: 1px solid var(--shiftai-break);
  color: {COLOR_BREAK_INK};
}}
.shiftai-gantt-bar--locked {{
  box-shadow: inset 0 0 0 2px {COLOR_FIXED};
}}
.shiftai-gantt-empty {{
  position: absolute;
  left: 0.35rem;
  top: 50%;
  transform: translateY(-50%);
  font-size: 0.68rem;
  color: rgba(49, 51, 63, 0.35);
}}
.shiftai-gantt-hours {{
  text-align: right;
  font-size: 0.74rem;
  color: rgba(49, 51, 63, 0.72);
  font-variant-numeric: tabular-nums;
}}
.shiftai-kbd {{
  border: 1px solid rgba(49, 51, 63, 0.2);
  border-radius: 0.25rem;
  padding: 0 0.25rem;
  font-size: 0.75rem;
  background: #f1f3f5;
}}
/* --- 説明の折りたたみ（hover / フォーカスで開くツールチップ） --- */
.shiftai-tip {{
  position: relative;
  display: inline-block;
  margin: 0.1rem 0;
  vertical-align: middle;
}}
.shiftai-tip > summary {{
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  cursor: help;
  list-style: none;
  border: 1px dashed rgba(49, 51, 63, 0.3);
  border-radius: 0.35rem;
  padding: 0.05rem 0.45rem;
  font-size: 0.78rem;
  color: rgba(49, 51, 63, 0.72);
  background: #fbfcfe;
}}
.shiftai-tip > summary:hover {{
  border-color: {COLOR_ACCENT};
  color: {COLOR_ACCENT};
  background: #f0f6ff;
}}
/* /details の既定マーカーを消して体裁を整える */
.shiftai-tip > summary::-webkit-details-marker {{ display: none; }}
.shiftai-tip > summary::marker {{ content: ""; }}
.shiftai-tip-body {{
  position: absolute;
  z-index: 100;
  left: 0;
  top: calc(100% + 0.35rem);
  width: 26rem;
  max-width: 78vw;
  padding: 0.6rem 0.8rem;
  border: 1px solid rgba(49, 51, 63, 0.18);
  border-left: 4px solid {COLOR_ACCENT};
  border-radius: 0.5rem;
  background: #ffffff;
  box-shadow: 0 6px 20px rgba(15, 23, 42, 0.14);
  font-size: 0.82rem;
  line-height: 1.7;
  color: {COLOR_INK};
  white-space: normal;
  text-align: left;
}}
.shiftai-tip-body p {{ margin: 0 0 0.35rem 0; }}
.shiftai-tip-body p:last-child {{ margin: 0; }}
/* 免責文は黄色系を保つ（中身は shiftai-tip-body のパネル内に置く） */
.shiftai-tip-body.shiftai-caveat-body {{
  border-left-color: #ffb74d;
  background: #fffdf5;
  font-weight: 400;
}}
@media (max-width: 700px) {{
  .shiftai-tip-body {{ position: static; width: auto; margin-top: 0.3rem; }}
}}
</style>
"""


def apply_page_config() -> None:
    """エントリポイントから最初の中立的な処理として 1 回だけ呼ぶこと。"""
    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon=PAGE_ICON,
        layout=LAYOUT,
        initial_sidebar_state="expanded",
    )


def inject_css() -> None:
    """カスタム CSS を読み込む。"""
    st.markdown(CSS, unsafe_allow_html=True)


def _escape(text: str) -> str:
    """HTML の特殊文字を実体参照に置き換える（ラベル埋め込み用の安全化）。

    ``label`` は呼び出し側の文字列をそのまま HTML に入れているため、
    ここが壊れると `<script>` などがそのまま描画される（XSS）。
    ``tests/test_security_hardening.py`` が実体参照の成否を検証する。
    """
    amp = "&" + "amp;"
    lt = "&" + "lt;"
    gt = "&" + "gt;"
    quot = "&" + "quot;"
    return str(text).replace("&", amp).replace("<", lt).replace(">", gt).replace('"', quot)


def tip(label: str, body: str) -> None:
    """開閉式の説明チップ（ツールチップ）を 1 つ描画する。

    初期画面の情報量は減らしたいが、説明を**削除してはいけない**。
    そこで「平常時は 1 行だけ見え、必要になった人だけ開ける」形にする。

    * ``label`` … 常時見える 1 行（例: 「❓ 使い方」）
    * ``body``  … hover / クリックで開く詳細（HTML 可。呼び出し側が整形する）
    """
    if not body:
        return
    st.markdown(
        '<details class="shiftai-tip">'
        f"<summary>{_escape(label)}</summary>"
        f'<div class="shiftai-tip-body">{body}</div>'
        "</details>",
        unsafe_allow_html=True,
    )


def render_footer() -> None:
    """ページ末尾のバージョン表示。"""
    st.markdown(
        f'<div class="shiftai-ver">{APP_ICON} {PAGE_TITLE} v{APP_VERSION}</div>',
        unsafe_allow_html=True,
    )


STEP_NAMES: tuple[str, ...] = tuple(step.label for step in EXPERT_STEPS)
"""上級者モード（5 タブ）のステップ。値は ``steps.EXPERT_STEPS`` から作る。"""

STEP_NAMES_SIMPLE: tuple[str, ...] = tuple(step.label for step in SIMPLE_STEPS)
"""シンプルモード（3 タブ）のステップ。値は ``steps.SIMPLE_STEPS`` から作る。"""

STEP_INDEX_SIMPLE: dict[int, int] = SIMPLE_INDEX
"""5 タブ番号からシンプルモード番号への書像（``steps.SIMPLE_INDEX`` の別名）。"""

MODE_KEY = "ui_simple_mode"
"""シンプルモード切替のキーは ``streamlit_app.MODE_KEY`` と共有する。"""


def simple_mode() -> bool:
    """シンプル 3 タブモードかどうか（既定: True）。"""
    return bool(st.session_state.get(MODE_KEY, True))


def step_names() -> tuple[str, ...]:
    """現在のモードに対応するステップ名の並びを返す。"""
    return tuple(step.label for step in steps_for_mode(simple_mode()))


def step_index(current: int) -> int:
    """5 タブ番号を、現在のモードのインデックスに変換する。

    シンプルモードでは ``steps.SIMPLE_INDEX`` で読み替える。未知番号は
    例外にせずそのまま返す（描画の途中で止まらないため）。
    """
    if simple_mode():
        return SIMPLE_INDEX.get(current, current)
    return current


def empty_state(message: str = "まずタブ1でデータを投入してください") -> None:
    """データ未投入時に呼び出す案内。"""
    st.info(message)


def current_step(current: int) -> Step:
    """5 タブ番号を、現在のモードの :class:`~shiftai.ui.steps.Step` へ変換する。

    上級者モードではそのまま、シンプルモードでは
    ``steps.SIMPLE_INDEX`` を経由して対応するステップへ読み替える。

    範囲外の番号でも例外にはしない。最後のステップを返す。
    """
    if not simple_mode():
        if 0 <= current < len(EXPERT_STEPS):
            return EXPERT_STEPS[current]
        return EXPERT_STEPS[-1]
    position = SIMPLE_INDEX.get(current, current)
    if 0 <= position < len(SIMPLE_STEPS):
        return SIMPLE_STEPS[position]
    return SIMPLE_STEPS[-1]


def heading(current: int) -> None:
    """タブの H3 見出しを描画する。番号と文は ``steps`` から取る。"""
    st.markdown(f"### {current_step(current).title}")


def tab_ref(current: int) -> str:
    """他の画面から現在のステップへ誘導する文言を返す（例: タブ2）。"""
    return current_step(current).tab_ref


MAIN_TABS_KEY = "main_tabs"
"""タブ列（``st.tabs``）が選択中タブを保持する ``session_state`` のキー。

``streamlit_app`` がこの名前でタブを作る。別モジュールからタブを
** Programmatically に** 切り替えるには、このキーにインデックスを
代入する（代入はウィジェット生成後ではなく **コールバックの中** で行う
必要がある。そうしないと Streamlit が例外を投げる）。
"""

#: シンプルモードに存在しないステップを、実際に開いているタブへ読み替える表。
#: 「必要人員」と「シフト表」はシンプルモードではタブ2（シフト作成）の中に入っている。
SIMPLE_TAB_ALIASES: dict[str, str] = {"requirements": "solve", "shift": "solve"}


def step_for_key(step_key: str) -> Step:
    """``step_key``（``steps`` の key）に対応する Step を返す。

    シンプルモードに無いステップ（``requirements`` / ``shift``）は
    :data:`SIMPLE_TAB_ALIASES` 把他のタブへ読み替える。未知の key は
    例外にせず最後のステップを返す（描画を止めないため）。
    """
    group = steps_for_mode(simple_mode())
    wanted = SIMPLE_TAB_ALIASES.get(step_key, step_key) if simple_mode() else step_key
    for step in group:
        if step.key == wanted:
            return step
    return group[-1]


def tab_position(step_key: str) -> int | None:
    """``step_key`` が tabs 並びの何番目か（0 始まり）を返す。

    そのステップがタブとして現れない構成なら ``None``。
    """
    group = steps_for_mode(simple_mode())
    wanted = SIMPLE_TAB_ALIASES.get(step_key, step_key) if simple_mode() else step_key
    for position, step in enumerate(group):
        if step.key == wanted:
            return position
    return None


def tab_ref_for(step_key: str) -> str:
    """``step_key`` を他の画面から指すときの文言（例: タブ4（シフト表・微調整））。"""
    step = step_for_key(step_key)
    return f"{step.tab_ref}（{step.label}）"


def goto_tab(step_key: str) -> None:
    """``step_key`` のタブを開くよう選択状態を立てる。

    **ウィジェット生成後のスクリプト本体からは呼ばない。**
    ``st.button(..., on_click=goto_tab, args=("shift",))`` のように
    コールバックから呼ぶこと（Streamlit の ``st.tabs`` は選択中インデックスを
    ``MAIN_TABS_KEY`` に保存するため、そこを書き換えるとタブが切り替わる）。
    タブとして現れないステップなら何もしない。
    """
    position = tab_position(step_key)
    if position is None:
        return
    st.session_state[MAIN_TABS_KEY] = position


STAGE_LABELS: dict[str, str] = {
    "empty": "① データ: 未入力",
    "loaded": "① データ: 読込済み ／ ② 必要人員: 未計算 ／ ③ シフト: 未作成",
    "ready": "② 必要人員: 計算済み ／ ③ シフト: 未作成",
    "solved": "③ シフト: 作成済み",
}
"""``state.STAGE_*`` を 1 行の説明にまとめた対応表。

``stale`` は内容が実行時に決まるので、ここには載せない。
"""


def render_status_bar() -> None:
    """すべてのタブより上に、現在の段階を 1 行で示す。

    ``state`` をモジュールレベルで import すると循環するため、
    関数の中で取り込む。
    """
    from shiftai.ui import state

    stage = state.pipeline_stage()
    if stage == state.STAGE_EMPTY:
        return
    if stage == state.STAGE_STALE:
        reason = state.get(state.KEY_STALE_REASON) or "入力が変更されました"
        label = f"⚠️ 再計算が必要です（{reason}）"
    else:
        label = STAGE_LABELS.get(stage, "")
    if not label:
        return
    with st.container(border=True):
        st.caption(label)


def step_indicator(current: int) -> None:
    """タブ上部に「今どのステップか」を示すパンくずを表示する。

    ``current`` は 0 始まりのステップ番号（タブ1 = 0）。
    シンプルモードでは 3 ステップに読み替えて描画する。
    """
    names = step_names()
    active = step_index(current)
    if not (0 <= active < len(names)):
        return
    chips(names, active)


def chips(names: Sequence[str], active: int, *, mark_done: bool = False) -> None:
    """進捗をチップ列で示す（``active`` の位置だけ強調）。

    ``mark_done=True`` は通過済みのステップに「✓」を付ける（ウィザード用）。
    ``step_indicator`` と同じ見た目のため、ステップ表示の見た目が一箇所に
    集約される。
    """
    rows = []
    for index, name in enumerate(names):
        text = str(name)
        if index == active:
            rows.append(
                f'<span class="shiftai-chip" style="background:{COLOR_ACCENT};color:#ffffff;'
                f'font-weight:700">{escape(text)}</span>'
            )
            continue
        if mark_done and index < active:
            text = f"{text} ✓"
        rows.append(
            f'<span class="shiftai-chip" style="background:#f1f3f5;'
            f'color:rgba(49,51,63,0.55)">{escape(text)}</span>'
        )
    st.markdown(
        '<div class="shiftai-legend">' + "".join(rows) + "</div>",
        unsafe_allow_html=True,
    )


def _position_of(current: int) -> int:
    """5 タブ番号を、現在のモードのステップ並びの中での位置へ変換する。

    ``SIMPLE_INDEX`` を使わず ``steps_for_mode`` の並びで位置を返すので、
    モードに存在しないステップ（シンプルモードの「必要人員」など）は
    自然に範囲外になる。
    """
    group = steps_for_mode(simple_mode())
    if not simple_mode():
        index = EXPERT_STEPS[current].index if 0 <= current < len(EXPERT_STEPS) else current
        return index
    position = SIMPLE_INDEX.get(current, current)
    if 0 <= position < len(group):
        return group[position].index
    return -1


def _next_action(current: int) -> str:
    """次にやることの 1 文を返す。最終ステップでは締めの文言を返す。"""
    group = steps_for_mode(simple_mode())
    position = _position_of(current)
    if position < 0 or position + 1 >= len(group):
        return "シフト表と出力ファイルを確認し、運用前に園長・設置責任者の承認を受けてください。"
    nxt = group[position + 1]
    return f"上の {nxt.tab_ref}（{nxt.label}）を開いてください。"


def next_step_hint(current: int) -> None:
    """タブ末尾に「次にやること」を示す導線を表示する。

    静的な文字列ではなく ``state.pipeline_stage()`` を見て決める。
    データが未投入のときにも「タブ2 へ進みましょう」と案内していたため、
    タブ2 側の「まずデータを投入してください」と循環していた。
    """
    from shiftai.ui import state

    stage = state.pipeline_stage()
    step = current_step(current)
    if step.key == "export":
        st.success(
            "🎉 これで全ステップ完了です。シフト表と出力ファイルを確認し、"
            "運用前に園長・設置責任者の承認を受けてください。"
        )
        return
    if stage == state.STAGE_STALE:
        reason = state.get(state.KEY_STALE_REASON) or "入力が変更されました"
        st.warning(f"⚠️ 再計算が必要です（{reason}）。{_next_action(current)}")
        return
    if stage == state.STAGE_EMPTY:
        st.info(
            "まだデータが投入されていません。"
            f"{tab_ref(0)}（{current_step(0).label}）から始めてください。"
        )
        return
    if stage in (state.STAGE_SOLVED, state.STAGE_READY) and step.key == "solve":
        st.success("✅ シフトを作成しました。内容を確認してから出力に進んでください。")
    st.info(f"👉 次のステップ: {_next_action(current)}")


def stuck_hint(current: int, reason: str) -> None:
    """通過できなかったときに、理由と次にどうするかを出す。

    ``reason`` は日本語の短文。``current`` は 5 タブ番号。
    """
    st.info(f"{reason}\n\n👉 {_next_action(current)}")


def where_is_period() -> str:
    """計画期間の入力場所を指す文言を返す。

    ウィザードが条件入力を受け持つときはタブ内、そうでなければサイドバー。
    キーは ``wizard.KEY_ANSWERED`` と同じ文字列 ``"wizard_answered"`` を使う。
    """
    if st.session_state.get("wizard_answered", False):
        return "このタブの「園の条件」で計画期間を設定してください。"
    return "サイドバーで計画期間を設定してください。"


def format_day(day: Any) -> str:
    """``9/28(月)`` 形式の日本語日付表記。ウィジェットの ``format_func`` 向け。"""
    return format_jp_date(day)


def caveat_box(*, collapsed: bool = True) -> None:
    """配置基準の数値には要確認である旨の注意書きを出す。

    ``collapsed=True``（既定）では 1 行の見出しだけに畳み、hover で全文が
    読めるようにする。配置基準の免責は重要だが、毎回全画面を占領すると
    「主要な数値が埋もれて見えなくなる」ため progressive disclosure にする。
    ``collapsed=False`` は従来どおり常時全文を出す（詳細ページ用）。
    """
    if not collapsed:
        st.markdown(
            f'<div class="shiftai-caveat">{CAVEAT_TEXT}</div>',
            unsafe_allow_html=True,
        )
        return
    st.markdown(
        '<details class="shiftai-tip">'
        "<summary>⚠️ この数値は要確認です（自治体の基準は施設ごとに異なります）</summary>"
        f'<div class="shiftai-tip-body shiftai-caveat-body">{CAVEAT_TEXT}</div>'
        "</details>",
        unsafe_allow_html=True,
    )


def rule_note_card(note: Any) -> None:
    """``LocalRuleNote`` 1 枚をカードとして描画する。

    ``title`` / ``detail`` / ``legal_reference`` / ``source`` は
    **エスケープしてから** 埋め込む。現状は定数のみが渡るため到達しないが、
    今後ノートの文字列が設定値（園が自由に編集できる項目）から来るようになれば
    ``unsafe_allow_html=True`` の再利用で任意マークアップが実行されうるため、
    境界で守る。
    """
    legal = getattr(note, "legal_reference", "") or ""
    source = getattr(note, "source", "") or ""
    title = getattr(note, "title", "") or getattr(note, "key", "")
    body = (
        f'<div class="shiftai-note-card">'
        f"<h5>{escape(str(title))}</h5>"
        f"<p>{escape(str(getattr(note, 'detail', '')))}</p>"
        f'<div class="shiftai-quote">{escape(str(legal))}</div>'
        f'<div class="shiftai-quote">出典: {escape(str(source))}</div>'
        f"</div>"
    )
    st.markdown(body, unsafe_allow_html=True)


def rule_note_cards(notes: Sequence[Any], *, title: str = "この基準の説明") -> None:
    """``LocalRuleNote`` 群を expander の中にカード描画する。"""
    with st.expander(title, expanded=False):
        caveat_box()
        for note in notes:
            rule_note_card(note)


def legend(items: Sequence[tuple[str, str]]) -> None:
    """凡例チップを描画する。

    ``label`` はエスケープする（現状は定数のみだが、凡例を外部から
    渡せるようにするために境界で守る）。
    ``color`` は CSS の ``background`` にそのまま入るので、
    ``style`` 属性 inject で外れないよう ``#`` で始まる値だけ許可する。
    """
    chips = "".join(
        f'<span class="shiftai-chip" style="background:{_safe_css_color(color)}">'
        f'<span style="color:#ffffff;{"font-weight:600" if color != COLOR_OFF else ""}">'
        f"{escape(str(label))}</span></span>"
        for label, color in items
    )
    st.markdown(f'<div class="shiftai-legend">{chips}</div>', unsafe_allow_html=True)


def _safe_css_color(color: Any) -> str:
    """``style="background:..."`` に入れても安全な色文字列だけを返す。

    ``#`` で始まる 3/6/8 桁の hex のみ通し、それ以外は既定色に戻す。
    ``;`` や ``"`` を含む文字列は CSS の属性を壊して
    別の ``style`` や属性を注入できるため、ここで落とす。
    """
    text = str(color).strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3})?(?:[0-9A-Fa-f]{2})?", text):
        return text
    return COLOR_OFF
