"""Timefold Benchmark Toolkit の単体テスト。"""

from __future__ import annotations

import pytest

from shiftai.benchmark import (
    create_scenario_overconstrained,
    create_scenario_small,
    format_benchmark_report,
    get_benchmark_corpus,
    run_benchmark,
)

pytestmark = pytest.mark.slow


def test_benchmark_corpus_generation():
    """シナリオ生成が正常に動作するか検証。"""
    scenarios = get_benchmark_corpus(quick=True)
    assert len(scenarios) >= 2
    sc_names = [s.name for s in scenarios]
    assert "Small-Standard" in sc_names
    assert "Overconstrained-Shortfall" in sc_names


def test_benchmark_run_and_report():
    """短時間でベンチマークが実行されレポートが生成されるか検証。"""
    sc = create_scenario_overconstrained(days_count=1)
    results = run_benchmark([sc], solvers=["greedy"], timeout_sec=3)
    assert len(results) == 1
    r = results[0]
    assert r.scenario_name == "Overconstrained-Shortfall"
    assert r.solver_name == "greedy"
    assert r.elapsed_sec > 0
    assert r.shortfall_slots >= 0

    report = format_benchmark_report(results)
    assert "NuShift Benchmark Report" in report
    assert "Overconstrained-Shortfall" in report
    assert "Medium スコア" in report


def test_benchmark_run_local_search_solver():
    """Timefold 局所探索（local_search）ソルバーがベンチマークで正常に計測できるか検証。"""
    sc = create_scenario_small(days_count=1)
    results = run_benchmark([sc], solvers=["local_search"], timeout_sec=3)
    assert len(results) == 1
    r = results[0]
    assert r.solver_name == "local_search"
    assert r.elapsed_sec > 0
    assert r.status in ("最適解", "実行可能解", "部分的なシフト")
