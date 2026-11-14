"""ConstraintRegistry の単体テスト。"""

from __future__ import annotations

from shiftai.constraints import ConstraintCategory, ConstraintRegistry


def test_全21制約が正しく登録されていること():
    all_constraints = ConstraintRegistry.all()
    assert len(all_constraints) >= 20

    # 代表的なハード制約の確認
    shortfall = ConstraintRegistry.get("SHORTFALL_STAFF")
    assert shortfall is not None
    assert shortfall.is_hard is True
    assert shortfall.category == ConstraintCategory.LEGAL_STATUTORY

    # 代表的なソフト制約の確認
    pref = ConstraintRegistry.get("WORK_ON_UNAVAILABLE")
    assert pref is not None
    assert pref.is_hard is False
    assert pref.category == ConstraintCategory.PREFERENCE


def test_カテゴリ別制約の抽出():
    legal = ConstraintRegistry.by_category(ConstraintCategory.LEGAL_STATUTORY)
    assert len(legal) >= 4
    assert all(c.category == ConstraintCategory.LEGAL_STATUTORY for c in legal)

    hard = ConstraintRegistry.hard_constraints()
    assert all(c.is_hard for c in hard)

    soft = ConstraintRegistry.soft_constraints()
    assert all(not c.is_hard for c in soft)
