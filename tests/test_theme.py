"""ui/theme.py の関数をテストする。

回帰防止のため、以下を検証する：
- CAVEAT_TEXT に自治体確認の文言が含まれていること
- format_day が正しく日付をフォーマットすること
- format_ratio が無限大を ∞ 表記にすること
- rule_note_cards が全プリセットで例外を投げないこと
- inject_css と legend が例外を投げないこと
"""

from __future__ import annotations

import pytest

from shiftai.ui import theme


def test_CAVEAT_TEXTに自治体確認の文言が含まれる() -> None:
    """免責文言が消失しないこと。"""
    assert "自治体" in theme.CAVEAT_TEXT
    assert "確認" in theme.CAVEAT_TEXT


def test_format_day() -> None:
    """日付を「月/日(曜日)」形式にフォーマットすること。"""
    assert theme.format_day(2026, 10, 1) == "10/1(木)"
    assert theme.format_day(2026, 1, 1) == "1/1(金)"
    assert theme.format_day(2026, 12, 31) == "12/31(木)"


def test_format_ratioは無限大を∞表記にする() -> None:
    """無限大を ∞ 表記にすること。"""
    assert theme.format_ratio(float("inf")) == "∞"
    assert theme.format_ratio(float("-inf")) == "-∞"
    assert theme.format_ratio(0.85) == "0.85"
    assert theme.format_ratio(1.2) == "1.2"
    assert theme.format_ratio(0) == "0"


def test_rule_note_cardsが全プリセットで例外を投げない() -> None:
    """全プリセットに対して rule_note_cards が例外を投げないこと。"""
    from shiftai import local_rules, standards

    for key, std in local_rules.MUNICIPAL_PRESETS.items():
        # UI を起動しないので、関数自体の例外のみを検証
        try:
            theme.rule_note_cards(std)
        except Exception as e:
            pytest.fail(f"rule_note_cards failed for {key}: {e}")


def test_inject_cssとlegendが例外を投げない() -> None:
    """AppTest 内で1フレーム流して検証する。"""
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(theme.inject_css)
    at.run()
    assert not at.exception

    at = AppTest.from_function(theme.legend)
    at.run()
    assert not at.exception