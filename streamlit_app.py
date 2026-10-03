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
    wizard,
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

MODE_KEY = theme.MODE_KEY
SIMPLE_GUIDE_BODY = (
    "<p><b>① データ</b> タブで「サンプルデータを読み込む」</p>"
    "<p><b>② シフト作成</b> タブで「🚀 シフトを自動作成する」を押す</p>"
    "<p><b>③ 出力</b> タブでファイルをダウンロード</p>"
    "<p>まずはサンプルデータで一通り試すのがおすすめです。</p>"
)
WIZARD_GUIDE_BODY = (
    "<p><b>① データ</b> タブのウィザードが 5 ステップで案内します</p>"
    "<p>園の条件 → 園児 → 職員 → 希望休 → 確認 の順に、必要なところだけ"
    "入力します。手元のデータがないときはサンプルデータで試せます。</p>"
    "<p><b>② シフト作成</b> タブで「🚀 シフトを自動作成する」を押す</p>"
    "<p><b>③ 出力</b> タブでファイルをダウンロード</p>"
)


def render_header() -> None:
    """ページタイトルと、常時 1 行だけの説明チップ。"""
    st.title(f"{APP_ICON} {APP_TITLE}")
    st.caption(SUBTITLE)
    if simple_mode():
        body = WIZARD_GUIDE_BODY if wizard_active() else SIMPLE_GUIDE_BODY
        theme.tip("❓ はじめての方へ（3 ステップ）", body)


def simple_mode() -> bool:
    """シンプル 3 タブモードかどうか（既定: True）。

    判定とステップ名の一覧は ``theme`` 側が真実（``streamlit_app`` から
    しか呼ばれないため循環参照は起きない）。
    """
    return theme.simple_mode()


def render_mode_switch() -> None:
    """入力方法・シンプル／上級者モードの切り替え（サイドバー内）。"""
    with st.sidebar:
        wizard.render_sidebar_control()
        st.toggle(
            "シンプルモード（初心者向け 3 タブ）",
            value=simple_mode(),
            key=MODE_KEY,
            help="OFF にすると、設定項目の多い従来の 5 タブ構成になります。",
        )


def wizard_active() -> bool:
    """入力方法としてウィザードが選ばれているか。

    最初の問い合わせ（``render_intro``）に答える前もウィザード扱いにする。
    どちらのモードでもタブ構成は変わらないため、副次的。
    """
    return wizard.use_wizard()


def render_data_tab() -> None:
    """タブ1（データ投入）を入力方法に応じて描画する。"""
    if not wizard.answered():
        wizard.render_intro()
        return
    if wizard_active():
        wizard.render()
        return
    tab_data.render()


def render_tabs() -> None:
    """モードに応じてタブを描画する。"""
    if simple_mode():
        tabs = st.tabs(list(SIMPLE_TABS), key="main_tabs")
        with tabs[0]:
            render_data_tab()
        with tabs[1]:
            tab_solve.render(auto_requirements=True, embed_shift=True)
        with tabs[2]:
            tab_export.render()
        return
    tabs = st.tabs(list(TAB_LABELS), key="main_tabs")
    with tabs[0]:
        render_data_tab()
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
    # ウィザードが「園の条件」を描くときは、同じキーのウィジェットを
    # 二重に作らないようサイドバー側を隠す。
    wizard_owns_conditions = wizard_active() and wizard.answered()
    sidebar.render(
        simple=simple_mode(),
        period=not wizard_owns_conditions,
        times=not wizard_owns_conditions,
    )
    # 状態バーは**サイドバーのあと**に描く。サイドバーが入力変更を
    # 検知して「再計算が必要」を記録するのはこの時点なので、先に描くと
    # 1 フレーム遅れて古い状態を表示する。
    theme.render_status_bar()
    render_tabs()
    theme.render_footer()


if __name__ == "__main__":
    main()
