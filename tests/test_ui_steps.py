"""``shiftai.ui.steps`` のデータ契約テスト。

UI/UX 改善 案1 で新設したステップ定義は、タブ名・見出し・導線文言の
唯一のの情報源である。ここが崩れると画面全体の番号がずれるため、
並び・値・モード間の整合を固定する。

並びと値をそのまま照合する検査を置いている。定義を書き換えたときは
必ずこのファイルにも反映させる。
"""

from __future__ import annotations

import pytest

from shiftai.ui import steps


def test_上級者モードは5ステップを持つ() -> None:
    """上級者モードは 5 ステップで、index が 0 から 4 と一致すること。"""
    assert len(steps.EXPERT_STEPS) == 5
    assert [step.index for step in steps.EXPERT_STEPS] == [0, 1, 2, 3, 4]


def test_シンプルモードは3ステップを持つ() -> None:
    """シンプルモードは 3 ステップであること。"""
    assert len(steps.SIMPLE_STEPS) == 3


def test_単純モードの書像は0から4をすべて被覆する() -> None:
    """上級者モードの番号 0 から 4 がすべて simple 側の番号に書かれること。"""
    assert set(steps.SIMPLE_INDEX) == set(range(5))
    for value in steps.SIMPLE_INDEX.values():
        assert 0 <= value < len(steps.SIMPLE_STEPS)


def test_ステップのキーはモード間で一致する() -> None:
    """simple 側の各要素の key が上級者側にも存在すること。"""
    expert_keys = {step.key for step in steps.EXPERT_STEPS}
    for step in steps.SIMPLE_STEPS:
        assert step.key in expert_keys


def test_単純モードは要員とシフト表を含まない() -> None:
    """3 タブ構成には「必要人員」も「シフト表」も存在しないこと。"""
    simple_keys = {step.key for step in steps.SIMPLE_STEPS}
    assert "requirements" not in simple_keys
    assert "shift" not in simple_keys


def test_ステップのキーに重複がない() -> None:
    """どちらのモードでも key が重複しないこと。"""
    for group in (steps.EXPERT_STEPS, steps.SIMPLE_STEPS):
        keys = [step.key for step in group]
        assert len(keys) == len(set(keys))


def test_見出しはすべて算用数字で始まる() -> None:
    """title は「数字 + 中黒 + 空白」で始まること。"""
    for group in (steps.EXPERT_STEPS, steps.SIMPLE_STEPS):
        for step in group:
            assert step.title[0].isdigit()
            assert step.title[1:3] == ". "


def test_短名はすべて丸数字で始まる() -> None:
    """label は丸数字（① から ⑤）で始まること。"""
    marks = "①②③④⑤"
    for group in (steps.EXPERT_STEPS, steps.SIMPLE_STEPS):
        for step in group:
            assert step.label[0] in marks


def test_旧テーマ定数と名前が一致する() -> None:
    """進捗チップの短い名前が従来の STEP_NAMES と一致すること。

    既存の ``tests/test_theme.py`` は別個の値を持ち回しているため、
    ここでは定義側を直接固定して二重管理を防ぐ。
    """
    labels = [step.label for step in steps.EXPERT_STEPS]
    assert labels == [
        "① データ投入",
        "② 必要人員",
        "③ シフト作成",
        "④ シフト表・微調整",
        "⑤ 出力",
    ]


def test_ステップ参照は現在のモードに従う() -> None:
    """simple モードの tab_ref はタブ1 からタブ3 に収まること。"""
    assert [step.tab_ref for step in steps.SIMPLE_STEPS] == ["タブ1", "タブ2", "タブ3"]
    assert [step.tab_ref for step in steps.EXPERT_STEPS] == [
        "タブ1",
        "タブ2",
        "タブ3",
        "タブ4",
        "タブ5",
    ]


def test_見出しはモードが変わっても同じであること() -> None:
    """同じステップはモードが違っても見出しの文言が変わらないこと。"""
    by_key = {step.key: step.title for step in steps.EXPERT_STEPS}
    for step in steps.SIMPLE_STEPS:
        assert step.title == by_key[step.key]


def test_未知のキーは例外になる() -> None:
    """知らない key は黙って既定に戻さず ValueError にすること。"""
    with pytest.raises(ValueError):
        steps.step_by_key("__nope__")


def test_モードで取り出す並びが切り替わる() -> None:
    """steps_for_mode が simple 引数に追従すること。"""
    assert steps.steps_for_mode(True) == steps.SIMPLE_STEPS
    assert steps.steps_for_mode(False) == steps.EXPERT_STEPS
