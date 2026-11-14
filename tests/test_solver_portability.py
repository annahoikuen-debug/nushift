"""ソルバーポータビリティ（HiGHS / CBC 自動検出 & 自己診断）のテスト。"""

from __future__ import annotations

import pytest

from shiftai.solver import (
    available_solvers,
    is_cbc_available,
    is_highs_available,
    select_optimal_solver,
    solver_diagnostic_info,
)


def test_is_cbc_available():
    """CBC ソルバーの検出がブール値を返すこと。"""
    res = is_cbc_available()
    assert isinstance(res, bool)


def test_is_highs_available():
    """HiGHS ソルバーの検出がブール値を返すこと。"""
    res = is_highs_available()
    assert isinstance(res, bool)


def test_select_optimal_solver_auto():
    """auto モードで最適なソルバーが選ばれること。"""
    name, available = select_optimal_solver("auto")
    assert isinstance(name, str)
    assert isinstance(available, bool)
    if is_highs_available():
        assert name == "HiGHS"
        assert available is True
    elif is_cbc_available():
        assert name == "PULP_CBC_CMD"
        assert available is True


def test_select_optimal_solver_explicit_highs():
    """HiGHS 明示指定時の判定。"""
    name, available = select_optimal_solver("highs")
    if is_highs_available():
        assert name == "HiGHS"
        assert available is True
    else:
        assert name == "PULP_CBC_CMD"


def test_solver_diagnostic_info():
    """ソルバー診断辞書が全項目を正しく返すこと。"""
    info = solver_diagnostic_info()
    assert "optimal_solver" in info
    assert "is_available" in info
    assert "highs_available" in info
    assert "cbc_available" in info
    assert "available_solvers" in info
    assert isinstance(info["available_solvers"], list)

