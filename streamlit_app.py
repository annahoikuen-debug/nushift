"""配置基準連動型 シフト自動作成AI — Streamlit エントリポイント。

このファイルは薄く保ち、レイアウトとタブの呼び出しだけを担う。
実処理は ``shiftai.ui`` 配下のモジュールにある。

既定は初心者向けの **シンプル 3 タブ** 構成。
「上級者モード」に切り替えると従来の 5 タブ構成になる。
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

SIMPLE_TABS: tuple[str, ...] = (
    "1️⃣ データ",
    "2️⃣ シフト作成",
    "3️⃣ 出力",
)
TAB_LABELS: tuple[str, ...] = (
    "1️⃣ データ投入",
    "2️⃣ 必要人員",
    "3️⃣ シフト作成",
    "4️⃣ シフト表・微調整",
    "5️⃣ 出力",
)

MODE_KEY = "ui_simple_mode"
SIMPLE_GUIDE = (
    "**はじめての方へ（3 ステップ）:**"
    " ①「データ」タブで **サンプルデータを読み込む** →"
    " ②「シフト作成」タブで **🚀 シフトを自動作成する** を押す →"
    " ③「出力」タブでファイルをダウンロード。"
    " まずはサンプルデータで一通り試すのがおすすめです。"
)


def render_header() -> None:
    """ページタイトルと説明。"""
    st.title(f"{APP_ICON} {APP_TITLE}")
    st.caption(SUBTITLE)
    if simple_mode():
        st.markdown(SIMPLE_GUIDE)


def simple_mode() -> bool:
    """シンプル 3 タブモードかどうか（既定: True）。"""
    return bool(st.session_state.get(MODE_KEY, True))


def render_mode_switch() -> None:
    """シンプル / 上級者モードの切り替え（サイドバー内）。"""
    with st.sidebar:
        st.toggle(
            "シンプルモード（初心者向け 3 タブ）",
            value=simple_mode(),
            key=MODE_KEY,
            help="OFF にすると、設定項目の多い従来の 5 タブ構成になります。",
        )


def render_tabs() -> None:
    """モードに応じてタブを描画する。"""
    if simple_mode():
        tabs = st.tabs(list(SIMPLE_TABS), key="main_tabs")
        with tabs[0]:
            tab_data.render()
        with tabs[1]:
            tab_solve.render(auto_requirements=True)
        with tabs[2]:
            tab_export.render()
        return
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
    render_mode_switch()
    sidebar.render(simple=simple_mode())
    render_tabs()
    theme.render_footer()


if __name__ == "__main__":
    main()
