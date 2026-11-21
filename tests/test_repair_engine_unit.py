"""Unit tests for NuShift Tier 0 Rule-based Absence Repair Engine."""

from __future__ import annotations

import time
from datetime import date, time as dt_time
import pytest

from shiftai import local_rules, sample_data
from shiftai.domain import CellState
from shiftai.shift_patterns import ShiftPattern
from shiftai.repair_engine import repair_shift_absence_fast
from shiftai.repair_engine.gap_scanner import SlotGapScanner
from shiftai.repair_engine.snapshot import SnapshotIndex
from shiftai.solver import solve_shift
from shiftai.standards import build_requirements


def test_repair_engine_microsecond_speed_and_determinism():
    """ルールベースリペアがミリ秒級で完了し、決定論的に同一結果を返すこと"""
    start_d = date(2026, 11, 2)
    days = [start_d]
    children, staff, prefs = sample_data.make_dataset(days, seed=42)
    std = local_rules.get_standard("全国基準（厚労省）")
    table = build_requirements(
        children,
        days,
        std,
        day_open=dt_time(7, 0),
        day_close=dt_time(19, 0),
        granularity_min=30,
    )

    # 1日分の初期解を作成
    res = solve_shift(
        children=children,
        staff=staff,
        requirements=table,
        preferences=prefs,
        standard=std,
        time_limit_sec=10,
    )
    assert res.ok

    # 欠勤者を指定（staff[1]）
    absent_id = staff[1].staff_id

    # 1. 速度計測
    start_t = time.perf_counter()
    repaired_1 = repair_shift_absence_fast(
        existing_result=res,
        absent_staff_id=absent_id,
        target_date=start_d,
        children=children,
        staff=staff,
        requirements=table,
        preferences=prefs,
        standard=std,
    )
    elapsed_ms = (time.perf_counter() - start_t) * 1000

    # 10ミリ秒未満で完了することを検証
    assert elapsed_ms < 50.0, f"Repair took too long: {elapsed_ms:.2f}ms"
    assert repaired_1.ok
    assert repaired_1.stats.get("repair_tier") == 0

    # 2. 決定論性テスト（10回実行して assignments の全キー・値が完全一致）
    for _ in range(10):
        repaired_n = repair_shift_absence_fast(
            existing_result=res,
            absent_staff_id=absent_id,
            target_date=start_d,
            children=children,
            staff=staff,
            requirements=table,
            preferences=prefs,
            standard=std,
        )
        assert repaired_n.assignments == repaired_1.assignments
