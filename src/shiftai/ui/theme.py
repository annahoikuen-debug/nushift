"""ページ設定・CSS・共通ウィジェット。

``apply_page_config`` はエントリポイントから最初に呼ばれる必要がある。
"""

from __future__ import annotations

from collections.abc import Sequence
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
.shiftai-kbd {{
  border: 1px solid rgba(49, 51, 63, 0.2);
  border-radius: 0.25rem;
  padding: 0 0.25rem;
  font-size: 0.75rem;
  background: #f1f3f5;
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
"""アプリの 5 ステップ。``step_indicator`` / ``next_step_hint`` で使う。"""


def empty_state(message: str = "まずタブ1でデータを投入してください") -> None:
    """データ未投入時に呼び出す案内。"""
    st.info(message)


def step_indicator(current: int) -> None:
    """タブ上部に「今どのステップか」を示すパンくずを表示する。

    ``current`` は 0 始まりのステップ番号（タブ1 = 0）。
    """
    if not (0 <= current < len(STEP_NAMES)):
        return
    chips = []
    for index, name in enumerate(STEP_NAMES):
        if index == current:
            chips.append(
                f'<span class="shiftai-chip" style="background:#1f6feb;color:#ffffff;'
                f'font-weight:700">{name}</span>'
            )
        else:
            chips.append(
                f'<span class="shiftai-chip" style="background:#f1f3f5;'
                f'color:rgba(49,51,63,0.55)">{name}</span>'
            )
    st.markdown(
        '<div class="shiftai-legend">' + "".join(chips) + "</div>",
        unsafe_allow_html=True,
    )


def next_step_hint(current: int) -> None:
    """タブ末尾に「次にやること」を示す導線を表示する。"""
    if current >= len(STEP_NAMES) - 1:
        st.success(
            "🎉 これで全ステップ完了です。シフト表と出力ファイルを確認し、"
            "運用前に園長・設置責任者の承認を受けてください。"
        )
        return
    nxt = STEP_NAMES[current + 1]
    st.info(f"👉 次のステップ: 上のタブ **{nxt}** を開いてください。")


def format_day(day: Any) -> str:
    """``9/28(月)`` 形式の日本語日付表記。ウィジェットの ``format_func`` 向け。"""
    return format_jp_date(day)


def caveat_box() -> None:
    """配置基準の数値には常に要確認である旨の注意書きを出す。"""
    st.markdown(
        f'<div class="shiftai-caveat">{CAVEAT_TEXT}</div>',
        unsafe_allow_html=True,
    )


def rule_note_card(note: Any) -> None:
    """``LocalRuleNote`` 1 枚をカードとして描画する。"""
    legal = getattr(note, "legal_reference", "") or ""
    source = getattr(note, "source", "") or ""
    title = getattr(note, "title", "") or getattr(note, "key", "")
    body = (
        f'<div class="shiftai-note-card">'
        f"<h5>{title}</h5>"
        f"<p>{getattr(note, 'detail', '')}</p>"
        f'<div class="shiftai-quote">{legal}</div>'
        f'<div class="shiftai-quote">出典: {source}</div>'
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
    """凡例チップを描画する。"""
    chips = "".join(
        f'<span class="shiftai-chip" style="background:{color}">'
        f'<span style="color:#ffffff;{"font-weight:600" if color != COLOR_OFF else ""}">'
        f"{label}</span></span>"
        for label, color in items
    )
    st.markdown(f'<div class="shiftai-legend">{chips}</div>', unsafe_allow_html=True)

