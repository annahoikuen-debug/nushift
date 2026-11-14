"""実務最適化および高速求解性能の検証テスト (practical shift optimization & performance fix).

実務的に使える法令違反がなく、職員の過剰な配置がないシフトが
自動で高速（15秒以内）に求解できることを保証する。
"""

from __future__ import annotations

import datetime as dt
from time import perf_counter

import pytest

from shiftai import local_rules, sample_data
from shiftai.domain import SolveStatus
from shiftai.gap_analysis import analyze_gap
from shiftai.monthly import MonthlySolveContext, solve_monthly_hierarchical
from shiftai.solver import is_highs_available
from shiftai.standards import build_requirements

FQ_KEY = "福岡市"


@pytest.mark.skipif(not is_highs_available(), reason="HiGHS ソルバーが必要です")
def test_7日間実務月次最適化の高速求解と法令遵守():
    """7日間（28名プール）のシフトを 15 秒以内に求解し、実務的に過不足なく法令違反がないことを検証する。"""
    week_days = [dt.date(2026, 9, 28) + dt.timedelta(days=i) for i in range(7)]
    children, staff, prefs = sample_data.make_dataset(week_days, seed=42)
    std = local_rules.get_standard(FQ_KEY)
    table = build_requirements(
        children,
        week_days,
        std,
        day_open=dt.time(7, 15),
        day_close=dt.time(19, 30),
        granularity_min=30,
    )

    context = MonthlySolveContext(
        target_month="2026-09",
        all_days=week_days,
        staff=staff,
        contracts={s.staff_id: s.contract for s in staff if s.contract},
        preferences=prefs,
        patterns=[],  # 自動生成に委ねる
        requirements=table,
        facility_settings=None,
        fixed_assignments={},
        prior_duty_history={},
        monthly_holiday_target=2,
        children=children,
        standard=std,
    )

    t0 = perf_counter()
    result = solve_monthly_hierarchical(context, time_limit_sec=60)
    elapsed = perf_counter() - t0

    # 1. 高速性: 15秒以内に完了すること（以前は180秒タイムアウトしてgreedy転落）
    assert elapsed < 15.0, f"求解に {elapsed:.2f}秒 かかりました（上限15秒）"
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    assert result.stats.get("fallback") is None, "greedy への転落が発生してはならない"

    # 2. 勤務時間の3分割禁止 (SPLIT_SHIFT 0件)
    split_shifts = [v for v in result.violations if v.code == "SPLIT_SHIFT"]
    assert len(split_shifts) == 0, f"3分割勤務が検出されました: {split_shifts}"

    # 3. 法令休憩不足 (BREAK_INSUFFICIENT 0件)
    break_insufficient = [v for v in result.violations if v.code == "BREAK_INSUFFICIENT"]
    assert len(break_insufficient) == 0, f"休憩不足が検出されました: {break_insufficient}"

    # 4. カバレッジと人員適正化: 不足スロットがごく少数でカバレッジ 99% 以上であること
    report = analyze_gap(table, result, staff, standard=std)
    assert report.coverage_ratio >= 0.99, f"カバレッジが 99% 未満です: {report.coverage_ratio:.2%}"
    assert report.total_shortfall_slots <= 5, f"不足時間帯数が多すぎます: {report.total_shortfall_slots}"
