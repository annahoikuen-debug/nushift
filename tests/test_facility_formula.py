"""認可外保育施設（企業主導型保育事業）の算出手法と、認可保育所側の挙動不変の検証。

**なぜこのテストが要るか**

配置基準の算出手法は制度ごとに違う。

* 認可保育所（本ツールの既定）: 年齢クラス毎に ``ceil(在園児数 / 定員比)``
* 認可外保育施設: 年齢区分ごとに小数第2位以下を切り捨て → **合算して +1** →
  小数第1位で四捨五入。1・2歳児と4歳以上児は**合算**してから割る。

出典は「企業主導型保育事業費補助金実施要綱」第3の2(4)②。公示されている
計算例（0〜2歳各5名→4名、0〜5歳各10名→9名）で検算できる。

認可保育所側で使.fields ``ceil`` を使い続けると認可外では結果が 1 名以上ずれる。
実測では 1 時間帯あたり最大 4 名ずれる例があり、**基準を満たしていると
誤判定する**ケース（公式値のほうが大きい）が約 0.5% ある。
そのため (1) 認可外は公式と一致すること、(2) 認可保育所は従来どおりであること
の両方をここで固定する。
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, time

import pytest

from shiftai import local_rules, standards
from shiftai.domain import (
    HEADCOUNT_MODES,
    QUALIFIED_MODES,
    AgeClass,
    AgeRatio,
    ChildPlan,
    StaffingStandard,
    trunc1,
)

DAY = date(2026, 10, 1)
OPEN = time(7, 15)
CLOSE = time(19, 30)

#: 認可外保育施設の年齢グループ（1・2歳児と4歳以上児は合算）。
FACILITY_AGE_GROUPS = (
    (AgeClass.INFANT,),
    (AgeClass.AGE_1, AgeClass.AGE_2),
    (AgeClass.AGE_3,),
    (AgeClass.AGE_4, AgeClass.AGE_5),
)

#: 実施要綱 第3の2(4)② の定員比（乳児3:1／1・2歳児6:1／3歳児20:1／4歳以上児30:1）。
FACILITY_RATIOS = {
    AgeClass.INFANT: 3.0,
    AgeClass.AGE_1: 6.0,
    AgeClass.AGE_2: 6.0,
    AgeClass.AGE_3: 20.0,
    AgeClass.AGE_4: 30.0,
    AgeClass.AGE_5: 30.0,
}


#: 認可保育所（厚労省告示第49号および自治体の告示ベース）のプリセット。
#: 制度切替（オプション A）で認可外保育施設のプリセットが追加されているが、
#: そちらは別の算法を使うためこの一覧から**外して**検証する。
LICENSED_PRESET_KEYS: tuple[str, ...] = (
    "全国基準（厚労省）",
    "東京都",
    "横浜市",
    "大阪市",
    "福岡市",
    "名古屋市",
    "京都市",
    "札幌市",
    "神戸市",
    "川崎市",
    "保育標準時間のみ園",
)

#: 認可外保育施設（企業主導型保育事業）のプリセット。
UNLICENSED_PRESET_KEYS: tuple[str, ...] = (
    "企業主導型保育事業（単独枠）",
    "企業主導型保育事業（保育事業者型・20名以上）",
)


def official_headcount(infant: int, age_12: int, age_3: int, age_4plus: int) -> int:
    """実施要綱の算式をそのまま再現する（外部実装の模範）。"""
    raw = trunc1(infant / 3) + trunc1(age_12 / 6) + trunc1(age_3 / 20) + trunc1(age_4plus / 30) + 1
    return int(math.floor(raw + 0.5))


def facility_standard(**overrides) -> StaffingStandard:
    """認可外保育施設（企業主導型保育事業・単独枠）相当の基準を作る。"""
    base = local_rules.get_standard("全国基準（厚労省）")
    settings: dict = {
        "name": "企業主導型保育事業（テスト）",
        "ratios": {age: AgeRatio(age, value, "ceil") for age, value in FACILITY_RATIOS.items()},
        "headcount_mode": "facility_formula",
        "headcount_extra": 1,
        "age_groups": FACILITY_AGE_GROUPS,
        "qualified_mode": "ratio",
        "min_qualified_ratio": 0.5,
        "min_qualified_floor": 1,
        "nurse_as_qualified_cap": 1,
    }
    settings.update(overrides)
    return replace(base, **settings)


def counts_of(infant: int, age_1: int, age_2: int, age_3: int, age_4: int, age_5: int):
    return {
        AgeClass.INFANT: infant,
        AgeClass.AGE_1: age_1,
        AgeClass.AGE_2: age_2,
        AgeClass.AGE_3: age_3,
        AgeClass.AGE_4: age_4,
        AgeClass.AGE_5: age_5,
    }


# ---------------------------------------------------------------------------
# 1. 認可外保育施設の算出手法が公式と一致すること
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("infant", "age_12", "age_3", "age_4plus", "expected"),
    [
        # 公示されている計算例（要綱の運用資料掲載）
        (5, 10, 0, 0, 4),
        (10, 20, 10, 20, 9),
        # 最低2名に届かないケース
        (0, 0, 0, 10, 1),
        # 1・2歳児・4歳以上児の合算が効くケース
        (0, 12, 0, 0, 3),
        (0, 0, 0, 30, 2),
        # 端数処理が効くケース
        (1, 0, 0, 0, 1),
        (0, 1, 0, 0, 1),
        (0, 0, 1, 0, 1),
        (0, 0, 0, 1, 1),
        (2, 0, 0, 0, 2),
        # 複数区分
        (3, 4, 5, 9, 3),
        (7, 7, 7, 20, 5),
        # 「1 名足りないと誤認する」ケース（per_class では 2 名と出る）
        (0, 0, 9, 0, 1),
        (0, 3, 20, 0, 3),
        (0, 0, 20, 0, 2),
    ],
)
def test_施設単位算出手法が要綱の算式と一致する(
    infant: int, age_12: int, age_3: int, age_4plus: int, expected: int
) -> None:
    standard = facility_standard()
    assert official_headcount(infant, age_12, age_3, age_4plus) == expected, (
        "テスト自体の検算例が公式と食い違う"
    )
    counts = counts_of(infant, age_12, 0, age_3, 0, age_4plus)
    assert standard.headcount_for_slot(counts) == expected


def test_要綱の計算例をそのまま再現できる() -> None:
    """公示資料の 2 例をそのまま通す（要綱の解釈が正しいことの担保）。"""
    # 例1: 0〜2歳児が各5名
    assert official_headcount(5, 10, 0, 0) == 4
    # 例2: 0〜5歳児が各10名
    assert official_headcount(10, 20, 10, 20) == 9


def test_端数処理は小数第2位以下を切り捨てて小数第1位で四捨五入する() -> None:
    standard = facility_standard()
    # 乳児 4名: 4/3 = 1.33 -> 1.3 -> +1 = 2.3 -> 2名
    assert standard.headcount_for_slot(counts_of(4, 0, 0, 0, 0, 0)) == 2
    # 乳児 5名: 5/3 = 1.66 -> 1.6 -> +1 = 2.6 -> 3名
    assert standard.headcount_for_slot(counts_of(5, 0, 0, 0, 0, 0)) == 3
    # 3歳児 10名: 0.5 -> +1 = 1.5 -> 2名（切り上げではなく四捨五入）
    assert standard.headcount_for_slot(counts_of(0, 0, 0, 10, 0, 0)) == 2


def test_在園児がいなければ0名を返す() -> None:
    standard = facility_standard()
    assert standard.headcount_for_slot(counts_of(0, 0, 0, 0, 0, 0)) == 0


# ---------------------------------------------------------------------------
# 2. 認可保育所側は従来どおりであること
# ---------------------------------------------------------------------------


def test_全プリセットは既定の算手法である() -> None:
    """プリセットの算手法が未定義の値にならないこと。"""
    for key, standard in local_rules.MUNICIPAL_PRESETS.items():
        assert standard.headcount_mode in HEADCOUNT_MODES, key
        assert standard.qualified_mode in QUALIFIED_MODES, key


def test_認可保育所のプリセットは従来どおりの算手法である() -> None:
    """認可保育所のプリセットが制度切替されていないこと。

    ここが崩れると既存の設定・出力・運用手順が黙って変わる。
    """
    for key in LICENSED_PRESET_KEYS:
        standard = local_rules.get_standard(key)
        assert standard.headcount_mode == "per_class", key
        assert standard.qualified_mode == "per_class", key
        assert standard.headcount_extra == 0, key
        assert standard.age_groups is None, key
        assert standard.nurse_as_qualified_cap == 0, key
        assert "要確認" in standard.remarks, key


def test_認可外保育施設のプリセットは要綱の算手法である() -> None:
    """制度切替したプリセットが要綱の要件どおりであること。"""
    for key in UNLICENSED_PRESET_KEYS:
        standard = local_rules.get_standard(key)
        assert standard.headcount_mode == "facility_formula", key
        assert standard.headcount_extra == 1, key
        assert standard.qualified_mode == "ratio", key
        assert standard.nurse_as_qualified_cap == 1, key
        assert standard.min_staff_per_room == 2, key
        assert standard.min_qualified_floor == 1, key
        assert standard.age_groups is not None, key
        flat = [ac for group in standard.age_groups for ac in group]
        assert sorted(flat, key=lambda a: a.sort_key) == list(AgeClass), key
        assert "要確認" in standard.remarks, key
        assert "第3の2(4)" in standard.remarks, key


def test_保育事業者型は四分の三単独枠は半数である() -> None:
    assert local_rules.get_standard(UNLICENSED_PRESET_KEYS[0]).min_qualified_ratio == 0.5
    assert local_rules.get_standard(UNLICENSED_PRESET_KEYS[1]).min_qualified_ratio == 0.75


@pytest.mark.parametrize("key", LICENSED_PRESET_KEYS)
def test_認可保育所は年齢クラス毎の切り上げのままである(key: str) -> None:
    standard = local_rules.get_standard(key)
    counts = counts_of(7, 8, 6, 5, 4, 4)
    per_class = sum(standard.headcount_for(ac, n) for ac, n in counts.items() if n > 0)
    assert standard.headcount_for_slot(counts) == per_class
    assert standard.allocate_staff(counts) == {
        ac: standard.headcount_for(ac, n) for ac, n in counts.items() if n > 0
    }
    for needed in range(1, 12):
        assert standard.slot_qualified_for(needed) == needed


def test_全国基準の必要人員の実測値が変わっていない() -> None:
    """``docs/03`` §8.1 に書いてある実測が維持されること。"""
    standard = local_rules.get_standard("全国基準（厚労省）")
    roster = {
        AgeClass.INFANT: 8,
        AgeClass.AGE_1: 7,
        AgeClass.AGE_2: 6,
        AgeClass.AGE_3: 5,
        AgeClass.AGE_4: 4,
        AgeClass.AGE_5: 4,
    }
    kids = [
        ChildPlan(f"c{index}", f"c{index}", DAY, age, time(9, 0), time(17, 0))
        for age, count in roster.items()
        for index in range(count)
    ]
    table = standards.build_requirements(kids, [DAY], standard, day_open=OPEN, day_close=CLOSE)
    rows = [r for r in table.for_day(DAY) if r.slot.label == "11:00-11:30"]
    assert sum(r.needed_staff for r in rows) == 9
    assert sum(r.needed_qualified for r in rows) == 9
    assert len(rows) == 6


# ---------------------------------------------------------------------------
# 3. 分配（表示用）が合計を壊さないこと
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("infant", "age_1", "age_2", "age_3", "age_4", "age_5"),
    [
        (0, 0, 0, 0, 0, 1),
        (1, 0, 0, 0, 0, 0),
        (0, 1, 1, 0, 0, 0),
        (5, 5, 5, 5, 5, 5),
        (10, 10, 10, 10, 10, 10),
        (0, 0, 3, 20, 0, 0),
        (2, 0, 0, 0, 0, 0),
        (0, 20, 0, 0, 0, 40),
    ],
)
def test_年齢クラスへの分配が合計を厳密に保つ(
    infant: int, age_1: int, age_2: int, age_3: int, age_4: int, age_5: int
) -> None:
    standard = facility_standard()
    counts = counts_of(infant, age_1, age_2, age_3, age_4, age_5)
    total = standard.headcount_for_slot(counts)
    alloc = standard.allocate_staff(counts)
    assert set(alloc) == {ac for ac, n in counts.items() if n > 0}
    assert sum(alloc.values()) == total

    qual_total = standard.slot_qualified_for(total)
    assert 0 <= qual_total <= total
    qual = standard.allocate_qualified(alloc, qual_total)
    assert set(qual) == set(alloc)
    assert sum(qual.values()) == qual_total
    for age_class, value in qual.items():
        assert value <= alloc[age_class], "必要保育士数が必要人員を超えていない"


def test_2名ルールは施設単位算出手法のあとに底上げされる() -> None:
    """底上げ前の合計が公式値、底上げ後が最低2名になること。"""
    standard = facility_standard()
    kids = [
        ChildPlan(f"c{i}", f"c{i}", DAY, AgeClass.AGE_4, time(9, 0), time(12, 0)) for i in range(10)
    ]
    with_rule = standards.build_requirements(
        kids, [DAY], standard, day_open=OPEN, day_close=CLOSE, enforce_min_two=True
    )
    without_rule = standards.build_requirements(
        kids, [DAY], standard, day_open=OPEN, day_close=CLOSE, enforce_min_two=False
    )
    slot = [r for r in without_rule.for_day(DAY) if r.slot.label == "10:00-10:30"]
    assert sum(r.needed_staff for r in slot) == official_headcount(0, 0, 0, 10)
    raised = [r for r in with_rule.for_day(DAY) if r.slot.label == "10:00-10:30"]
    assert sum(r.needed_staff for r in raised) == 2


# ---------------------------------------------------------------------------
# 4. 資格要件（保育士比率・みなし保育士）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("needed", "ratio", "floor_q", "expected"),
    [
        (2, 0.5, 1, 1),
        (3, 0.5, 1, 2),
        (4, 0.5, 1, 2),
        (5, 0.5, 1, 3),
        (3, 0.75, 1, 3),
        (4, 0.75, 1, 3),
        (5, 0.75, 1, 4),
        (2, 0.5, 2, 2),
        (1, 0.5, 1, 1),
    ],
)
def test_必要保育士数が比率と下限で決まる(
    needed: int, ratio: float, floor_q: int, expected: int
) -> None:
    standard = facility_standard(min_qualified_ratio=ratio, min_qualified_floor=floor_q)
    assert standard.slot_qualified_for(needed) == expected


def test_保育事業者型は保育士比率4分の3以上になる() -> None:
    single = facility_standard()
    shared = facility_standard(min_qualified_ratio=0.75)
    needed = 8
    assert single.slot_qualified_for(needed) == 4
    assert shared.slot_qualified_for(needed) == 6


def test_必要人数が0なら必要保育士数も0() -> None:
    assert facility_standard().slot_qualified_for(0) == 0


# ---------------------------------------------------------------------------
# 5. 不正な設定の拒否
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["per_class", "facility_formula"])
def test_正しいモードは受け付けられる(mode: str) -> None:
    assert facility_standard(headcount_mode=mode).headcount_mode == mode


def test_未知の算出手法は拒否する() -> None:
    with pytest.raises(ValueError, match="headcount_mode が不正"):
        facility_standard(headcount_mode="unknown")
    with pytest.raises(ValueError, match="qualified_mode が不正"):
        facility_standard(qualified_mode="unknown")


@pytest.mark.parametrize("ratio", [-0.1, 1.1])
def test_保育士比率の範囲外は拒否する(ratio: float) -> None:
    with pytest.raises(ValueError, match="min_qualified_ratio"):
        facility_standard(min_qualified_ratio=ratio)


def test_定数と上限の負数は拒否する() -> None:
    with pytest.raises(ValueError, match="headcount_extra"):
        facility_standard(headcount_extra=-1)
    with pytest.raises(ValueError, match="nurse_as_qualified_cap"):
        facility_standard(nurse_as_qualified_cap=-1)


# ---------------------------------------------------------------------------
# 6. 説明文（basis）に算式が残ること
# ---------------------------------------------------------------------------


def test_根拠文に算式と資格比率が含まれる() -> None:
    standard = facility_standard()
    kids = [
        ChildPlan(f"c{i}", f"c{i}", DAY, AgeClass.INFANT, time(9, 0), time(12, 0)) for i in range(7)
    ]
    table = standards.build_requirements(kids, [DAY], standard, day_open=OPEN, day_close=CLOSE)
    rows = [r for r in table.for_day(DAY) if r.slot.label == "10:00-10:30"]
    basis = rows[0].basis
    assert "小数第2位以下切捨て" in basis
    assert "四捨五入" in basis
    assert "＋ 1" in basis
    assert "うち保育士2名（比率50%）" in basis
