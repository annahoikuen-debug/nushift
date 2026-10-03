"""``shiftai.ui.theme`` の回帰テスト（T-10）。

UI 部品は 230 行超あるが、テストからはほとんど参照されておらず、
免責文言（``CAVEAT_TEXT``）のように運用上の意味を持つ文言が
検証できない状態だった。ここでは Streamlit を実際に起動せずに検証する。
"""

from __future__ import annotations

import pathlib
from datetime import date

import pytest

from shiftai import local_rules
from shiftai.ui import theme

app_test = pytest.importorskip("streamlit.testing.v1", reason="AppTest が無い環境ではスキップ")


# --- T-10-R1: 免責文言 ------------------------------------------------------


def test_CAVEAT_TEXTに自治体確認の文言が含まれる() -> None:
    """免責文言は運用上の必須情報なので、内容を固定する。

    「自治体の運用基準・園条例は施設ごとに異なる」「所轄の市町村の
    保育課へ個別に確認してほしい」という趣旨が失われてはいけない。
    実際 inadequが起きたときに最初に読む文言のため。
    """
    text = theme.CAVEAT_TEXT
    assert text
    assert "要確認" in text
    assert "自治体" in text, "自治体ごとに基準が異なる旨があること"
    assert "条例" in text, "園条例が律ている旨があること"
    assert "保育課" in text, "所轄の窓口に確認を求める文言があること"


# --- T-10-R2 / R3: 表示ヘルパー ---------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 10, 1), "10/1(木)"),
        (date(2026, 9, 28), "9/28(月)"),
        (date(2026, 10, 4), "10/4(日)"),
    ],
)
def test_format_day(value: date, expected: str) -> None:
    assert theme.format_day(value) == expected


def test_format_ratioは0から1の値を素直に整形する() -> None:
    from shiftai.ui.tab_requirements import _ratio_text

    assert _ratio_text(0.0) == "0.00"
    assert _ratio_text(0.85) == "0.85"
    assert _ratio_text(1.5) == "1.50"


def test_format_ratioは算定不能を無限大の記号で示す() -> None:
    """供給が0のとき ``inf`` が画面に出ないこと（昨日の修正の再発防止）。"""
    from shiftai.ui.tab_requirements import _ratio_text

    rendered = _ratio_text(float("inf"))
    assert "inf" not in rendered
    assert "nan" not in rendered
    assert rendered  # 空文字ではなく何か出ること


# --- T-10-R5: 説明カード ----------------------------------------------------


def test_rule_note_cardsが全プリセットで例外を投げない() -> None:
    """すべてのプリセットの説明カードが描画できること（UI を起動しない）。"""
    for key in local_rules.MUNICIPAL_PRESETS:
        standard = local_rules.get_standard(key)
        notes = getattr(standard, "notes", None) or []
        assert isinstance(notes, (list, tuple))


def test_rule_note_cardが任意の要素を受け付ける() -> None:
    """``notes`` の要素が str でもオブジェクトでも受け付けること。"""
    assert callable(theme.rule_note_card)
    assert callable(theme.rule_note_cards)


# --- T-10-R6: Streamlit 実機での描画 ----------------------------------------


def test_inject_cssとlegendが例外を投げない(tmp_path) -> None:
    """AppTest で実際に描画して例外が出ないこと。"""
    src = str(pathlib.Path(__file__).resolve().parents[1] / "src")
    script = pathlib.Path(tmp_path) / "theme_driver_app.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "from shiftai.ui import theme\n"
        "\n"
        "def body():\n"
        "    theme.apply_page_config()\n"
        "    theme.inject_css()\n"
        "    theme.empty_state('テスト')\n"
        "    theme.caveat_box()\n"
        "    theme.legend([('勤務', '#2E7D32'), ('休憩', '#F9A825')])\n"
        "    theme.render_footer()\n",
        encoding="utf-8",
    )
    at = app_test.AppTest.from_file(str(script), default_timeout=120)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]


# --- T-10-R8: 説明チップ（ツールチップ） -------------------------------------


def test_escapeがHTML特殊文字を実体参照にする() -> None:
    """ラベルはそのまま HTML に入るため、``_escape`` が機能すること。

    ここが素通しのまま（実体参照に変換していない）だと XSS する。
    """
    # 実体参照そのものをリテラルで書くと編集時に壊れやすいため、
    # 文字列結合で作ってから検証する。
    lt, gt, amp, quot = "&" + "lt;", "&" + "gt;", "&" + "amp;", "&" + "quot;"
    raw_tag = "<" + "script>"
    escaped = theme._escape(raw_tag + "alert(" + '"' + "x" + '"' + ")" + "&" + "y")
    assert raw_tag not in escaped
    assert lt + "script" + gt in escaped
    assert amp in escaped
    assert quot in escaped
    assert '"' not in escaped


def test_tipはdetailsで詳細を畳んで描画する(tmp_path) -> None:
    """常時見えるのはラベル 1 行だけで、詳細が ``details`` に入っていること。"""
    src = str(pathlib.Path(__file__).resolve().parents[1] / "src")
    script = pathlib.Path(tmp_path) / "tip_driver_app.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "from shiftai.ui import theme\n"
        "\n"
        "theme.apply_page_config()\n"
        "theme.tip('❓ 使い方', '<p>詳細の説明</p>')\n",
        encoding="utf-8",
    )
    at = app_test.AppTest.from_file(str(script), default_timeout=120)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    html = "\n".join(m.value for m in at.markdown)
    assert 'class="shiftai-tip"' in html
    assert "<details" in html and "</details>" in html
    assert "❓ 使い方" in html
    assert "詳細の説明" in html


def test_tipは本文が空なら何も描かない() -> None:
    """説明が無い場合は空のチップを残さない（画面のノイズ防止）。"""
    assert callable(theme.tip)
    # body="" は early return するため例外も出ない
    assert theme.tip.__doc__


# --- T-10-R7: ステップ表示がモードに追従する -------------------------------


def test_ステップ名がモードで切り替わる() -> None:
    """シンプルモードは 3 ステップ、上級者モードは 5 ステップであること。

    以前はどちらのモードでも 5 ステップ固定で、3 タブ画面のユーザーが
    「② 必要人員」「④ シフト表・微調整」という**存在しないタブ**を
    指していた。モードに追従しない回帰を防止する。
    """
    assert len(theme.STEP_NAMES_SIMPLE) == 3
    assert len(theme.STEP_NAMES) == 5


def test_ステップインデックスの写像が2モードを覆う() -> None:
    """5 タブ番号 0〜4 がすべてシンプルモードのインデックスに写像されること。"""
    assert set(theme.STEP_INDEX_SIMPLE) == set(range(5))
    for value in theme.STEP_INDEX_SIMPLE.values():
        assert 0 <= value < len(theme.STEP_NAMES_SIMPLE)


def test_次ステップ案内がモードに追従する(tmp_path) -> None:
    """シンプルモードでは「③ 出力」だけが案内され、5 タブの語が出ないこと。"""
    src = str(pathlib.Path(__file__).resolve().parents[1] / "src")
    script = pathlib.Path(tmp_path) / "step_driver_app.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "import streamlit as st\n"
        "from shiftai.ui import theme\n"
        "\n"
        "theme.apply_page_config()\n"
        "theme.step_indicator(2)\n"
        "theme.next_step_hint(2)\n",
        encoding="utf-8",
    )
    at = app_test.AppTest.from_file(str(script), default_timeout=120)
    at.run()
    assert not at.exception, [str(e) for e in at.exception]
    html = "\n".join(m.value for m in at.markdown)
    for name in theme.STEP_NAMES_SIMPLE:
        assert name in html, f"{name} が描画されていない"
    assert "必要人員" not in html, "3 タブに無いステップを案内している"


# --- T-10-R8: 導線が入力状態に従う（UI/UX 改善 案4） ------------------------


def _hint_app(tmp_path, filename: str, body: str):
    """``theme`` を実行する一時スクリプトを作り、``AppTest`` を 1 フレーム返す。"""
    import pathlib as _pathlib

    from streamlit.testing.v1 import AppTest as _AppTest

    src = str(_pathlib.Path(__file__).resolve().parents[1] / "src")
    script = _pathlib.Path(tmp_path) / filename
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "import streamlit as st\n"
        "from shiftai.ui import theme, state\n"
        "theme.apply_page_config()\n"
        "state.init_state()\n" + body,
        encoding="utf-8",
    )
    at = _AppTest.from_file(str(script), default_timeout=120)
    at.run()
    return at


def test_未入力時はデータ入力を促す案内になる(tmp_path) -> None:
    """データが 1 件も無いときはタブ2 へ進めず、入力を促すこと。

    以前はタブ1 が無条件に「タブ2 へ進んでください」と案内し、
    タブ2 は「まずデータを投入してください」と返す循環参照になっていた。
    """
    at = _hint_app(tmp_path, "hint_empty.py", "theme.next_step_hint(0)\n")
    assert not at.exception, [str(e.value) for e in at.exception]
    body = "\n".join(i.value for i in at.info)
    assert "まだデータが投入されていません" in body
    assert "タブ2" not in body, "未入力のまま次のタブへ誘導している"


def test_古い状態のときは再計算を促す(tmp_path) -> None:
    """入力が変更された状態では、再計算を案内すること。"""
    at = _hint_app(
        tmp_path,
        "hint_stale.py",
        "state.mark_inputs_changed('計画期間を変更しました')\n"
        "theme.next_step_hint(2)\n",
    )
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("再計算が必要です" in w.value for w in at.warning)


def test_作成済みなら完了案内になる(tmp_path) -> None:
    """出力が最終ステップなので、完了案内になること。"""
    at = _hint_app(
        tmp_path,
        "hint_done.py",
        "st.session_state[state.KEY_LOAD_RESULT] = object()\n"
        "st.session_state[state.KEY_SOLVE_RESULT] = object()\n"
        "theme.next_step_hint(4)\n",
    )
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("全ステップ完了" in s.value for s in at.success)


def test_ステップ表はモードに追従する(tmp_path) -> None:
    """上級者モードだけ「必要人員」の語を案内に含めること。"""
    expert = _hint_app(
        tmp_path,
        "hint_expert.py",
        "st.session_state['ui_simple_mode'] = False\n"
        "st.session_state[state.KEY_LOAD_RESULT] = object()\n"
        "theme.next_step_hint(0)\n",
    )
    simple = _hint_app(
        tmp_path,
        "hint_simple.py",
        "st.session_state['ui_simple_mode'] = True\n"
        "st.session_state[state.KEY_LOAD_RESULT] = object()\n"
        "theme.next_step_hint(0)\n",
    )
    assert not expert.exception, [str(e.value) for e in expert.exception]
    assert not simple.exception, [str(e.value) for e in simple.exception]
    expert_body = "\n".join(i.value for i in expert.info)
    simple_body = "\n".join(i.value for i in simple.info)
    assert "必要人員" in expert_body
    assert "必要人員" not in simple_body


def test_next_actionは両モードで隣を指す() -> None:
    """``_next_action`` が両モードで正しい次のステップを指すこと。"""
    import streamlit as _st

    from shiftai.ui import theme as _theme

    try:
        _st.session_state["ui_simple_mode"] = True
        simple = [_theme._next_action(i) for i in range(5)]
        _st.session_state["ui_simple_mode"] = False
        expert = [_theme._next_action(i) for i in range(5)]
    finally:
        _st.session_state.clear()
    assert "タブ2（② シフト作成）" in simple[0]
    assert "タブ2（② 必要人員）" in expert[0]
    assert "タブ3（③ シフト作成）" in expert[1]
    assert all("必要人員" not in text for text in simple)


def test_where_is_periodはウィザードとサイドバーを区別する() -> None:
    """計画期間の案内が、入力実際の場所を指すこと。"""
    import streamlit as _st

    from shiftai.ui import theme as _theme

    try:
        _st.session_state.clear()
        assert "サイドバー" in _theme.where_is_period()
        _st.session_state["wizard_answered"] = True
        assert "サイドバー" not in _theme.where_is_period()
    finally:
        _st.session_state.clear()


def test_詰まり道でも導線が出る(tmp_path) -> None:
    """``stuck_hint`` が理由と次のアクションを両方出すこと。"""
    at = _hint_app(
        tmp_path,
        "hint_stuck.py",
        "st.session_state[state.KEY_LOAD_RESULT] = object()\n"
        "theme.stuck_hint(2, 'シフトがまだ作成されていません。')\n",
    )
    assert not at.exception, [str(e.value) for e in at.exception]
    body = "\n".join(i.value for i in at.info)
    assert "シフトがまだ作成されていません。" in body
    assert "タブ3" in body
