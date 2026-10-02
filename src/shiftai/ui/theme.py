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
    COLOR_BREAK,
    COLOR_OFF,
    COLOR_OVER,
    COLOR_SHORTFALL,
    COLOR_WORK,
)
from shiftai.domain import format_jp_date

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
  color: #1f2430;
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
  color: #1f2430;
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
.shiftai-shortfall {{ background-color: {COLOR_SHORTFALL}22; color: #8e0000; font-weight: 600; }}
.shiftai-overstaff {{ background-color: {COLOR_OVER}1a; color: #0d47a1; }}
.shiftai-ok {{ background-color: {COLOR_WORK}1a; color: #1b5e20; }}
.shiftai-fixed {{ box-shadow: inset 0 0 0 2px #6a1b9a; }}
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
  color: #7a5200;
}}
.shiftai-gantt-bar--locked {{
  box-shadow: inset 0 0 0 2px #6a1b9a;
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
  border-color: #1f6feb;
  color: #1f6feb;
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
  border-left: 4px solid #1f6feb;
  border-radius: 0.5rem;
  background: #ffffff;
  box-shadow: 0 6px 20px rgba(15, 23, 42, 0.14);
  font-size: 0.82rem;
  line-height: 1.7;
  color: #1f2430;
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


STEP_NAMES: tuple[str, ...] = (
    "① データ投入",
    "② 必要人員",
    "③ シフト作成",
    "④ シフト表・微調整",
    "⑤ 出力",
)
"""上級者モード（5 タブ）のステップ。"""

STEP_NAMES_SIMPLE: tuple[str, ...] = (
    "① データ",
    "② シフト作成",
    "③ 出力",
)
"""シンプルモード（3 タブ）のステップ。"""

#: 5 タブの番号 → シンプルモードの番号。どちらのモードでも
#: 同じ呼び出し側（``step_indicator(2)`` など）で済むように写像する。
STEP_INDEX_SIMPLE: dict[int, int] = {0: 0, 1: 1, 2: 1, 3: 1, 4: 2}

MODE_KEY = "ui_simple_mode"
"""シンプルモード切替のキーは ``streamlit_app.MODE_KEY`` と共有する。"""


def simple_mode() -> bool:
    """シンプル 3 タブモードかどうか（既定: True）。"""
    return bool(st.session_state.get(MODE_KEY, True))


def step_names() -> tuple[str, ...]:
    """現在のモードに対応するステップ名の並びを返す。"""
    return STEP_NAMES_SIMPLE if simple_mode() else STEP_NAMES


def step_index(current: int) -> int:
    """5 タブ番号を、現在のモードのインデックスに変換する。"""
    if simple_mode():
        return STEP_INDEX_SIMPLE.get(current, current)
    return current


def empty_state(message: str = "まずタブ1でデータを投入してください") -> None:
    """データ未投入時に呼び出す案内。"""
    st.info(message)


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
                f'<span class="shiftai-chip" style="background:#1f6feb;color:#ffffff;'
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


def next_step_hint(current: int) -> None:
    """タブ末尾に「次にやること」を示す導線を表示する。"""
    names = step_names()
    active = step_index(current)
    if active >= len(names) - 1:
        st.success(
            "🎉 これで全ステップ完了です。シフト表と出力ファイルを確認し、"
            "運用前に園長・設置責任者の承認を受けてください。"
        )
        return
    nxt = names[active + 1]
    st.info(f"👉 次のステップ: 上のタブ **{nxt}** を開いてください。")


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
    渡せるようにするMoment のために境界で守る）。
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
