"""週次 hierarchical エンジン（solve_weekly）の回帰テスト。

レビュー指摘1（Stage 1 が週次 7 日ホライズンで必違約となり全員 OFF を
返す）の regression を検知する。また preferences=None クラッシュと
legacy フォールバックの挙動を担保する。
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from shiftai import local_rules, sample_data
from shiftai.domain import CellState, SolveStatus
from shiftai.standards import build_requirements
from shiftai.weekly import solve_weekly

FQ_KEY = "福岡市"


def _small_inputs(days: list[date]):
    """少数職員・短い期間のサンプルデータ（求解を数秒に抑える）。"""
    children, staff, prefs = sample_data.make_dataset(days, seed=42)
    std = local_rules.get_standard(FQ_KEY)
    table = build_requirements(
        children,
        days,
        std,
        day_open=time(7, 15),
        day_close=time(19, 30),
        granularity_min=30,
    )
    return children, staff, prefs, table


def _working_cells(result) -> int:
    """OFF 以外の割当セル数（勤務が生成されているかの指標）。"""
    n = 0
    for sd in result.shift_days:
        for by_slot in sd.assignments.values():
            for state in by_slot.values():
                if state != CellState.OFF:
                    n += 1
    return n


pytestmark = pytest.mark.slow


def test_週次階層エンジンが全員OFFにならない():
    """指摘1 regression: Stage 1 が週次で必違約 → 全員 OFF にならないこと。"""
    days = [date(2026, 11, 16) + timedelta(days=i) for i in range(3)]
    children, staff, prefs, table = _small_inputs(days)
    std = local_rules.get_standard(FQ_KEY)

    result = solve_weekly(
        children,
        staff,
        table,
        prefs,
        time_limit_sec=30,
        standard=std,
        engine="hierarchical",
        parallel_workers=1,
    )

    assert result.status != SolveStatus.ERROR, result.messages
    assert result.stats.get("solver") == "hierarchical_monthly"
    # 全員OFF（指摘1の症状）でないこと。勤務セルが1つ以上存在する
    assert _working_cells(result) > 0, "週次階層エンジンが全員 OFF を返した（指摘1の復帰）"


def test_preferencesなしでもクラッシュしない():
    """指摘2 regression: preferences=None で Stage 1 内がクラッシュしないこと。"""
    days = [date(2026, 11, 16) + timedelta(days=i) for i in range(3)]
    children, staff, _prefs, table = _small_inputs(days)
    std = local_rules.get_standard(FQ_KEY)

    result = solve_weekly(
        children,
        staff,
        table,
        None,
        time_limit_sec=30,
        standard=std,
        engine="hierarchical",
        parallel_workers=1,
    )
    assert result.status != SolveStatus.ERROR, result.messages


def test_legacyフォールバックで解が返る():
    """engine="legacy" が solve_shift にフォールバックし解が返ること。"""
    days = [date(2026, 11, 16) + timedelta(days=i) for i in range(3)]
    children, staff, prefs, table = _small_inputs(days)
    std = local_rules.get_standard(FQ_KEY)

    result = solve_weekly(
        children,
        staff,
        table,
        prefs,
        time_limit_sec=30,
        standard=std,
        engine="legacy",
    )
    assert result.status != SolveStatus.ERROR, result.messages
    assert _working_cells(result) > 0
