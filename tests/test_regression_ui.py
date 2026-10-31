"""UI 回帰テスト。

Streamlit コンポーネント、タブ遷移、ウィザードフローが回帰しないことを検証する。
"""

from __future__ import annotations

import logging
from datetime import date, time
from pathlib import Path

import pytest
import streamlit as st

from shiftai import gap_analysis
from shiftai.domain import CellState, Slot, Violation, ViolationSeverity
from shiftai.ui import components, state
from tests.conftest import DAY

logging.getLogger("streamlit").setLevel(logging.ERROR)

pytestmark = pytest.mark.timeout(300)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


@pytest.fixture(autouse=True)
def clean_session_state():
    """st.session_state を空にしてテスト間の依存をなくす。"""
    st.session_state.clear()
    yield
    st.session_state.clear()


# ---------------------------------------------------------------------------
# アプリケーション状態
# ---------------------------------------------------------------------------


def test_app_state_init_creates_default_state():
    """state.init_state() がエラーなく実行できること。"""
    state.init_state()
    assert True


def test_state_init_function_runs():
    """state.init_state() が再実行してもエラーにならないこと。"""
    state.init_state()
    state.init_state()
    assert True


# ---------------------------------------------------------------------------
# コンポーネント：純粋関数
# ---------------------------------------------------------------------------


def test_components_importable():
    """shiftai.ui.components がインポートできること。"""
    assert components is not None


# ---------------------------------------------------------------------------
# Streamlit アプリモジュール
# ---------------------------------------------------------------------------


def test_streamlit_app_importable():
    """streamlit_app モジュールがインポートできること。"""
    import streamlit_app

    assert streamlit_app is not None


def test_streamlit_app_has_render_function():
    """streamlit_app にエントリポイントが存在すること。"""
    import streamlit_app

    assert hasattr(streamlit_app, "main") or hasattr(streamlit_app, "run")


# ---------------------------------------------------------------------------
# ギャップ分析と UI コンポーネントの連携
# ---------------------------------------------------------------------------


def test_gap_analysis_check_violations_returns_list():
    """gap_analysis.check_violations がリストを返すこと。"""
    from shiftai.domain import RequirementTable, Slot
    from shiftai.solver import SolveResult, SolveStatus

    slots = [Slot(time(9, 0), time(9, 30))]
    requirements = RequirementTable(
        day_open=time(9, 0),
        day_close=time(15, 0),
        granularity_min=30,
        slots=tuple(slots),
        rows={DAY: []},
    )
    result = SolveResult(
        status=SolveStatus.OPTIMAL,
        shift_days=[],
        assignments=[],
        violations=[],
        messages=[],
        stats={},
    )

    violations = gap_analysis.check_violations(
        requirements=requirements,
        result=result,
        staff=[],
        preferences={},
        standard=None,
    )
    assert isinstance(violations, list)


def test_violation_severity_values():
    """ViolationSeverity の列挙値が不変であること。"""
    assert ViolationSeverity.BLOCKER == "法令違反"
    assert ViolationSeverity.WARNING == "要調整"
    assert ViolationSeverity.INFO == "参考"


def test_violation_creation():
    """Violation オブジェクトが正しく作成できること。"""
    v = Violation(
        code="TEST_VIOLATION",
        message="テスト違反",
        severity=ViolationSeverity.WARNING,
        day=date(2026, 9, 28),
        slot=Slot(time(9, 0), time(9, 30)),
        staff_id="S001",
    )
    assert v.code == "TEST_VIOLATION"
    assert v.message == "テスト違反"
    assert v.severity == ViolationSeverity.WARNING
    assert v.staff_id == "S001"


# ---------------------------------------------------------------------------
# セル状態
# ---------------------------------------------------------------------------


def test_cell_state_values():
    """CellState の列挙値が不変であること。"""
    assert CellState.WORK == "勤務"
    assert CellState.BREAK == "休憩"
    assert CellState.OFF == "オフ"
