"""配置基準連動型 シフト自動作成AI — Streamlit エントリポイント。

このファイルは薄く保ち、レイアウトとタブの呼び出しだけを担う。
実処理は ``shiftai.ui`` 配下のモジュールにある。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import streamlit as st

from shiftai.config import APP_ICON, APP_TITLE
from shiftai.ui import (
    sidebar,
    state,
    tab_data,
    tab_export,
    tab_requirements,
    tab_shift,
    tab_solve,
    theme,
)

SUBTITLE = "園児の登降園予定 × 保育所の職員配置基準 → 適法なシフトを自動生成"
TAB_LABELS: tuple[str, ...] = (
    "1️⃣ データ投入",
    "2️⃣ 必要人員",
    "3️⃣ シフト作成",
    "4️⃣ シフト表・微調整",
    "5️⃣ 出力",
)


def render_header() -> None:
    """ページタイトルと説明。"""
    st.title(f"{APP_ICON} {APP_TITLE}")
    st.caption(SUBTITLE)
    st.markdown(
        "**3 ステップで使えます。**"
        " ① タブ1 でデータを投入 → ② タブ2 で必要人員を計算 → "
        "③ タブ3 で「シフトを自動作成する」を押す。"
    )


def render_tabs() -> None:
    """5 つのタブを描画する。"""
    tabs = st.tabs(list(TAB_LABELS), key="main_tabs")
    with tabs[0]:
        tab_data.render()
    with tabs[1]:
        tab_requirements.render()
    with tabs[2]:
        tab_solve.render()
    with tabs[3]:
        tab_shift.render()
    with tabs[4]:
        tab_export.render()


def main() -> None:
    """アプリの本体。"""
    theme.apply_page_config()
    state.init_state()
    theme.inject_css()
    render_header()
    sidebar.render()
    render_tabs()
    theme.render_footer()


if __name__ == "__main__":
    main()
