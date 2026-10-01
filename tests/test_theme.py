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

app_test = pytest.importorskip(
    "streamlit.testing.v1", reason="AppTest が無い環境ではスキップ"
)


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
    assert rendered  # 空文字ではなく何か 나오ること


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
