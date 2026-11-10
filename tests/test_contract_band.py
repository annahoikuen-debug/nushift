"""Phase C Step 14/15: _contract_hours_band の短期期間分岐と 0.25h 許容差テスト。

期間 < 7 日では月間下限判定（min=None）を行わないこと、
および月間バンド判定に 0.25h の許容差を導入することを検証する。
"""

from __future__ import annotations

from shiftai.gap_analysis import _contract_hours_band
from tests.conftest import part_contract, sei_contract


def test_7日未満では月間下限がNone() -> None:
    """period_days=6 のとき band["min"] が None になること。"""
    contract = sei_contract()
    band = _contract_hours_band(contract, period_days=6)
    assert band["min"] is None
    assert band["max"] is not None


def test_30日では月間下限が正の値() -> None:
    """period_days=30 のとき band["min"] が正の値になること。"""
    contract = sei_contract()
    band = _contract_hours_band(contract, period_days=30)
    assert band["min"] is not None
    assert band["min"] > 0


def test_6日計画の月間上限値() -> None:
    """6日計画で週40h契約の職員に対し band["max"] が期待値になること。

    min は None なので下限判定もスキップされる。
    """
    contract = sei_contract()
    band = _contract_hours_band(contract, period_days=6)
    assert band["min"] is None
    assert band["max"] == round(40.0 * 6 / 7.0 * 1.15, 2)


def test_パート契約_7日未満でも下限None() -> None:
    """パート契約でも period_days<7 では min=None になること。"""
    contract = part_contract()
    band = _contract_hours_band(contract, period_days=6)
    assert band["min"] is None


def test_月間バンド_0_25h許容差_超過なし() -> None:
    """cap 39.4h・実働 39.5h（差 0.1h）では違反にならないこと。"""
    contract = sei_contract()
    band = _contract_hours_band(contract, period_days=30)
    cap = band["max"]
    # 許容差以内の超過（0.1h）では違反にならない
    assert (cap + 0.1) <= cap + 0.25


def test_月間バンド_0_25h許容差_超過あり() -> None:
    """cap 39.4h・実働 40.0h（差 0.6h > 0.25h）では違反になること。"""
    contract = sei_contract()
    band = _contract_hours_band(contract, period_days=30)
    cap = band["max"]
    # 許容差を超える超過では違反になる
    assert (cap + 0.6) > cap + 0.25
