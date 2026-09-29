"""自治体ローカルルール（``shiftai.local_rules``）のテスト。

プリセット一覧・上書き・動的登録・比較表・「設定説明カード」の法令根拠を保証する。
"""

from __future__ import annotations

from datetime import time

import pytest

from shiftai import local_rules
from shiftai.domain import AgeClass, AgeRatio, StaffingStandard
from shiftai.local_rules import (
    DEFAULT_PRESET_KEY,
    MUNICIPAL_PRESETS,
    LocalRuleNote,
    build_standard,
    get_standard,
    list_presets,
    local_rule_notes,
    preset_source,
    preset_summary,
    register_preset,
    standard_to_dataframe,
)

pytestmark = pytest.mark.timeout(300)

LEGAL_TOKENS = ("児童福祉法", "労働基準法")


@pytest.fixture
def sandbox_registry():
    """``register_preset`` で作ったプリセットをテスト後に必ず破棄する。"""
    added: list[str] = []
    yield added
    for key in added:
        MUNICIPAL_PRESETS.pop(key, None)
        local_rules._SUMMARIES.pop(key, None)
        local_rules._SOURCES.pop(key, None)


# ---------------------------------------------------------------------------
# プリセット一覧
# ---------------------------------------------------------------------------


def test_プリセットは11件以上():
    """主要自治体＋全国基準が登録されていること。"""
    assert len(MUNICIPAL_PRESETS) >= 11
    assert len(list_presets()) == len(MUNICIPAL_PRESETS)


def test_全国基準と福岡市が存在する():
    """テストが前提とする 2 つのプリセットが必ずあること。"""
    assert get_standard("全国基準（厚労省）").name == "全国基準（厚労省）"
    assert get_standard("福岡市").name == "福岡市"
    assert DEFAULT_PRESET_KEY in MUNICIPAL_PRESETS


@pytest.mark.parametrize("key", sorted(MUNICIPAL_PRESETS))
def test_各プリセットのratioが6年齢クラスを全て持つ(key):
    """0〜5 歳児の定員比が全て定義されていること（未定義だと計算で落ちる）。"""
    standard = get_standard(key)
    for age_class in AgeClass:
        ratio = standard.ratio_for(age_class)
        assert ratio.children_per_staff > 0
        assert ratio.rounding in ("ceil", "floor", "round")


def test_get_standard_不明キーはKeyError():
    """不明なプリセットキーは握り潰さず ``KeyError`` にすること。"""
    with pytest.raises(KeyError):
        get_standard("存在しない市")
    with pytest.raises(KeyError):
        get_standard("")


def test_list_presets_各要素がkey_name_summaryを持つ():
    """UI の selectbox が前提とするキーが全て揃っていること。"""
    for entry in list_presets():
        assert set(entry) >= {"key", "name", "summary"}
        assert entry["key"]
        assert entry["name"]
        assert entry["summary"]


def test_プリセットkeyとnameが一致する():
    """キーがそのまま ``StaffingStandard.name`` であること（UI の出し分け）。"""
    for entry in list_presets():
        assert entry["key"] == entry["name"]


def test_全プリセットのremarksに要確認の文言がある():
    """自治体基準は運用前に必ず個別確認を要する旨が書いてあること。"""
    for key, standard in MUNICIPAL_PRESETS.items():
        assert "要確認" in standard.remarks, key


# ---------------------------------------------------------------------------
# 上書き・動的登録
# ---------------------------------------------------------------------------


def test_build_standard_差分だけ反映():
    """``overrides`` で指定した項目だけが変わり、他は元のままであること。"""
    base = get_standard("全国基準（厚労省）")
    patched = build_standard("全国基準（厚労省）", {"min_staff_per_room": 3})
    assert patched.min_staff_per_room == 3
    assert base.min_staff_per_room == 2
    assert patched.standard_time == base.standard_time
    assert patched.late_care_relaxed == base.late_care_relaxed


def test_build_standard_ratiosはマージ():
    """``ratios`` は差分だけ渡しても他年齢クラスの比を保つこと。"""
    patched = build_standard(
        "全国基準（厚労省）", {"ratios": {AgeClass.INFANT: 4.0}}
    )
    assert patched.ratio_for(AgeClass.INFANT).children_per_staff == pytest.approx(4.0)
    assert patched.ratio_for(AgeClass.AGE_1).children_per_staff == pytest.approx(6.0)


def test_build_standard_AgeRatioも受け付ける():
    """``AgeRatio`` オブジェクトを直接渡しても反映されること。"""
    patched = build_standard(
        "全国基準（厚労省）",
        {"ratios": {AgeClass.AGE_3: AgeRatio(AgeClass.AGE_3, 10.0, "floor")}},
    )
    assert patched.ratio_for(AgeClass.AGE_3).rounding == "floor"
    assert patched.ratio_for(AgeClass.AGE_3).children_per_staff == pytest.approx(10.0)


def test_build_standard_未知の項目はKeyError():
    """``StaffingStandard`` に無い項目名は明示的にエラーにすること。"""
    with pytest.raises(KeyError):
        build_standard("全国基準（厚労省）", {"unknown_field": 1})


def test_build_standard_不正なratiosはTypeError():
    """ratios が Mapping でなければ ``TypeError`` にすること。"""
    with pytest.raises(TypeError):
        build_standard("全国基準（厚労省）", {"ratios": [1, 2, 3]})


def test_build_standard_空のoverridesはプリセットそのまま():
    """``overrides=None`` ではオブジェクトを複製せず返すこと（同一性）。"""
    base = get_standard("福岡市")
    assert build_standard("福岡市", None) is base
    assert build_standard("福岡市", {}) is base


def test_register_presetで追加できる(sandbox_registry):
    """UI から動的に登録したプリセットが一覧と取得に出ること。"""
    key = "テスト市（架空）"
    sandbox_registry.append(key)
    standard = StaffingStandard(
        name=key,
        ratios={age: AgeRatio(age, 5.0, "ceil") for age in AgeClass},
        remarks="テスト用の架空基準（要確認）",
    )
    register_preset(key, standard, summary="テスト用のサマリー", source="テスト出典")
    assert get_standard(key) is standard
    assert any(e["key"] == key for e in list_presets())
    entry = next(e for e in list_presets() if e["key"] == key)
    assert entry["summary"] == "テスト用のサマリー"
    assert entry["source"] == "テスト出典"
    assert len(standard_to_dataframe()) == len(MUNICIPAL_PRESETS)


def test_register_preset_上書きできる():
    """既存キーを上書き登録できること（元プリセットは元に戻して他テストに漏らさない）。"""
    key = "全国基準（厚労省）"
    original = MUNICIPAL_PRESETS[key]
    original_summary = local_rules._SUMMARIES[key]
    original_source = local_rules._SOURCES[key]
    try:
        register_preset(key, original, summary="一時的な上書き", source="テスト")
        entry = next(e for e in list_presets() if e["key"] == key)
        assert entry["summary"] == "一時的な上書き"
    finally:
        MUNICIPAL_PRESETS[key] = original
        local_rules._SUMMARIES[key] = original_summary
        local_rules._SOURCES[key] = original_source


# ---------------------------------------------------------------------------
# 比較表
# ---------------------------------------------------------------------------


def test_standard_to_dataframeの行数():
    """比較表がプリセット 1 行ずつであること。"""
    frame = standard_to_dataframe()
    assert len(frame) == len(MUNICIPAL_PRESETS)
    assert list(frame["キー"]) == list(MUNICIPAL_PRESETS)


def test_standard_to_dataframeの列():
    """UI の比較表が前提とする列が揃っていること。"""
    frame = standard_to_dataframe()
    for column in ("キー", "名称", "保育標準時間", "早朝保育", "延長保育",
                   "延長緩和措置", "最低配置人数", "出典", "備考"):
        assert column in frame.columns


def test_standard_to_dataframeの延長緩和表記():
    """延長緩和措置が「可/不可」の文字列になっていること。"""
    frame = standard_to_dataframe().set_index("キー")
    assert frame.loc["福岡市", "延長緩和措置"] == "可"
    assert frame.loc["全国基準（厚労省）", "延長緩和措置"] == "不可"
    assert frame.loc["福岡市", "延長緩和の期限"] == "18:30"
    assert frame.loc["全国基準（厚労省）", "延長緩和の期限"] == "—"


# ---------------------------------------------------------------------------
# 設定説明カード
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(MUNICIPAL_PRESETS))
def test_local_rule_notesがLocalRuleNoteを返す(key):
    """各プリセットで説明カードが 1 枚以上生成されること。"""
    notes = local_rule_notes(get_standard(key))
    assert notes
    assert all(isinstance(n, LocalRuleNote) for n in notes)
    assert all(n.title and n.detail for n in notes)


@pytest.mark.parametrize("key", sorted(MUNICIPAL_PRESETS))
def test_法令名を含む根拠がある(key):
    """少なくとも 1 枚のカードが法令名（児童福祉法/労働基準法）を引用していること。"""
    notes = local_rule_notes(get_standard(key))
    joined = "\n".join(n.legal_reference for n in notes)
    assert any(token in joined for token in LEGAL_TOKENS)


@pytest.mark.parametrize("key", sorted(MUNICIPAL_PRESETS))
def test_注意カードが含まれる(key):
    """「要確認」の注意カード（caveat）と法令カード（legal）が必ず存在すること。"""
    notes = local_rule_notes(get_standard(key))
    assert any(n.key == "caveat" for n in notes)
    assert any(n.key == "legal" for n in notes)
    assert "要確認" in next(n for n in notes if n.key == "caveat").source
    assert "保証はない" in next(n for n in notes if n.key == "caveat").detail


def test_2名ルールのカードがある():
    """2名ルールの説明カードが含まれること。"""
    notes = {n.key: n for n in local_rule_notes(get_standard("全国基準（厚労省）"))}
    assert "min_two" in notes
    assert "2名" in notes["min_two"].detail


def test_短時間保育のカードがある():
    """短時間保育（保育標準時間のみ）の説明カードが含まれること。"""
    notes = {n.key: n for n in local_rule_notes(get_standard("保育標準時間のみ園"))}
    assert "short_time" in notes
    assert "短時間" in notes["short_time"].detail


def test_延長保育のカードが緩和策を説明する():
    """福岡市の延長カードが代替措置の期限（18:30）を示すこと。"""
    notes = {n.key: n for n in local_rule_notes(get_standard("福岡市"))}
    assert "late_care" in notes
    assert "18:30" in notes["late_care"].detail
    assert "支援員" in notes["late_care"].detail


# ---------------------------------------------------------------------------
# サマリー・出典
# ---------------------------------------------------------------------------


def test_preset_summaryとsource():
    """プリセットの 1 行サマリーと出典が文字列で返ること。"""
    standard = get_standard("福岡市")
    assert isinstance(preset_summary(standard), str)
    assert preset_summary(standard)
    assert isinstance(preset_source(standard), str)
    assert "福岡" in preset_source(standard)


def test_未登録の基準はremarksから生成():
    """プリセット以外の ``StaffingStandard`` でも summary/source が例外なく返ること。"""
    ad_hoc = StaffingStandard(
        name="架空園", ratios={age: AgeRatio(age, 4.0, "ceil") for age in AgeClass},
        remarks="架空の基準",
    )
    assert "4:1" in preset_summary(ad_hoc)
    assert preset_source(ad_hoc) == "架空の基準"


def test_default_ratiosは全国基準と一致():
    """``default_ratios()`` が全国基準プリセットの比と一致すること。"""
    expected = get_standard("全国基準（厚労省）").ratios
    for age_class, ratio in local_rules.default_ratios().items():
        assert ratio.children_per_staff == pytest.approx(expected[age_class].children_per_staff)


def test_default_ratiosは新しい辞書を返す():
    """``default_ratios()`` を書き換えても他に影響しないこと。"""
    first = local_rules.default_ratios()
    first[AgeClass.INFANT] = AgeRatio(AgeClass.INFANT, 99.0, "ceil")
    second = local_rules.default_ratios()
    assert second[AgeClass.INFANT].children_per_staff == pytest.approx(3.0)


def test_保育標準時間のみ園は短時間保育専用():
    """短時間保育のみ園プリセットが ``is_short_time_only`` を立てること。"""
    standard = get_standard("保育標準時間のみ園")
    assert standard.is_short_time_only is True
    assert standard.standard_time[0] <= time(9, 0)
