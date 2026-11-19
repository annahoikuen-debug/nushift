"""Timefold Benchmark Toolkit 思想に基づく体系的ベンチマーク＆プロファイラ。

園規模別（Small/Medium/Large）および難易度別（標準、競合、過剰制約・人手不足）の
標準シナリオコーパスに対して、各ソルバー（HiGHS/CBC/Greedy/Monthly）の
求解速度・スコア推移・制約充足率を自動計測・比較する。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from time import perf_counter
from typing import Any

from shiftai.domain import (
    ChildPlan,
    FacilitySettings,
    RequirementTable,
    SolveStatus,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
)
from shiftai.local_rules import get_standard
from shiftai.sample_data import make_children, make_dataset, make_preferences, make_staff
from shiftai.solver import solve_shift
from shiftai.standards import build_requirements


@dataclass(frozen=True)
class BenchmarkScenario:
    """ベンチマーク評価用シナリオ定義。"""

    name: str
    description: str
    days: list[date]
    children: list[ChildPlan]
    staff: list[StaffMember]
    preferences: dict[str, StaffPreferences]
    requirements: RequirementTable
    settings: FacilitySettings
    standard: StaffingStandard


@dataclass
class BenchmarkResult:
    """シナリオに対するソルバー実行結果の計測値。"""

    scenario_name: str
    solver_name: str
    status: SolveStatus
    elapsed_sec: float
    hard_score: int
    medium_score: float
    soft_score: float
    is_feasible: bool
    blocker_count: int
    warning_count: int
    shortfall_slots: int
    stats: dict[str, Any] = field(default_factory=dict)


def create_scenario_small(days_count: int = 7) -> BenchmarkScenario:
    """小規模園シナリオ（職員10名、適正人員、標準的希望休）。"""
    start = date(2026, 6, 1)
    days = [start + timedelta(days=i) for i in range(days_count)]
    all_staff = make_staff(seed=42)
    # 10名のみ選抜（常勤6名、パート4名）
    staff = all_staff[:10]
    children = make_children(days, seed=42)
    # 園児数を小規模向けに調整（1クラス3〜5名程度）
    small_children = [c for i, c in enumerate(children) if i % 2 == 0]
    prefs = make_preferences(staff, days, seed=42)
    settings = FacilitySettings()
    standard = get_standard("全国基準（厚労省）")
    reqs = build_requirements(
        small_children,
        days,
        standard,
        day_open=settings.day_open,
        day_close=settings.day_close,
    )

    return BenchmarkScenario(
        name="Small-Standard",
        description="小規模認可園 (職員10名/7日間/適正人員)",
        days=days,
        children=small_children,
        staff=staff,
        preferences=prefs,
        requirements=reqs,
        settings=settings,
        standard=standard,
    )


def create_scenario_medium(days_count: int = 7) -> BenchmarkScenario:
    """中規模園シナリオ（職員24名、希望休競合、標準的園児数）。"""
    start = date(2026, 6, 1)
    days = [start + timedelta(days=i) for i in range(days_count)]
    children, staff, prefs = make_dataset(days, seed=100)
    settings = FacilitySettings()
    standard = get_standard("全国基準（厚労省）")
    reqs = build_requirements(
        children,
        days,
        standard,
        day_open=settings.day_open,
        day_close=settings.day_close,
    )

    return BenchmarkScenario(
        name="Medium-Competing",
        description="中規模標準園 (職員24名/7日間/希望休競合あり)",
        days=days,
        children=children,
        staff=staff,
        preferences=prefs,
        requirements=reqs,
        settings=settings,
        standard=standard,
    )


def create_scenario_overconstrained(days_count: int = 3) -> BenchmarkScenario:
    """過剰制約・人手不足シナリオ（需要 > 供給、欠員スラック検証用）。"""
    start = date(2026, 6, 1)
    days = [start + timedelta(days=i) for i in range(days_count)]
    children = make_children(days, seed=200)
    # 職員数を極端に減らす（必要数の半分以下）
    all_staff = make_staff(seed=200)
    short_staff = all_staff[:4]
    prefs = make_preferences(short_staff, days, seed=200)
    settings = FacilitySettings()
    standard = get_standard("全国基準（厚労省）")
    reqs = build_requirements(
        children,
        days,
        standard,
        day_open=settings.day_open,
        day_close=settings.day_close,
    )

    return BenchmarkScenario(
        name="Overconstrained-Shortfall",
        description="過剰制約極限 (職員4名/3日間/需要過多・欠員スラック検証)",
        days=days,
        children=children,
        staff=short_staff,
        preferences=prefs,
        requirements=reqs,
        settings=settings,
        standard=standard,
    )


def get_benchmark_corpus(quick: bool = True) -> list[BenchmarkScenario]:
    """ベンチマークシナリオ集を返す。"""
    scenarios = [
        create_scenario_small(days_count=3 if quick else 7),
        create_scenario_overconstrained(days_count=2 if quick else 3),
    ]
    if not quick:
        scenarios.append(create_scenario_medium(days_count=7))
    return scenarios


def run_benchmark(
    scenarios: Sequence[BenchmarkScenario],
    solvers: Sequence[str] = ("auto", "greedy"),
    timeout_sec: int = 10,
) -> list[BenchmarkResult]:
    """各シナリオに対して指定ソルバーを実行し計測結果を返す。"""
    results: list[BenchmarkResult] = []

    for sc in scenarios:
        for s_name in solvers:
            start_t = perf_counter()
            try:
                if s_name in ("local_search", "timefold_lahc"):
                    from shiftai.greedy import solve_shift_greedy
                    from shiftai.local_search import ruin_and_recreate

                    g_res = solve_shift_greedy(
                        children=sc.children,
                        staff=sc.staff,
                        requirements=sc.requirements,
                        preferences=sc.preferences,
                        settings=sc.settings,
                        standard=sc.standard,
                    )
                    res = ruin_and_recreate(
                        initial_result=g_res,
                        requirements=sc.requirements,
                        staff=sc.staff,
                        preferences=sc.preferences,
                        settings=sc.settings,
                        standard=sc.standard,
                        max_iterations=20,
                        lahc_size=10,
                    )
                else:
                    res = solve_shift(
                        children=sc.children,
                        staff=sc.staff,
                        requirements=sc.requirements,
                        preferences=sc.preferences,
                        settings=sc.settings,
                        standard=sc.standard,
                        solver_name=s_name,
                        time_limit_sec=timeout_sec,
                    )
                elapsed = perf_counter() - start_t
                hard = res.score.hard_score if res.score else (0 if res.ok else -1)
                medium = res.score.medium_score if res.score else 0.0
                soft = res.score.soft_score if res.score else 0.0
                feasible = res.score.is_feasible if res.score else res.ok
                blockers = sum(
                    1 for v in res.violations if getattr(v.severity, "name", "") == "BLOCKER"
                )
                warnings = sum(
                    1 for v in res.violations if getattr(v.severity, "name", "") == "WARNING"
                )
                shortfalls = len(res.overconstrained_shortfalls)
                b_res = BenchmarkResult(
                    scenario_name=sc.name,
                    solver_name=s_name,
                    status=res.status,
                    elapsed_sec=round(elapsed, 3),
                    hard_score=hard,
                    medium_score=round(medium, 1),
                    soft_score=round(soft, 1),
                    is_feasible=feasible,
                    blocker_count=blockers,
                    warning_count=warnings,
                    shortfall_slots=shortfalls,
                    stats=res.stats,
                )
            except Exception as exc:
                elapsed = perf_counter() - start_t
                b_res = BenchmarkResult(
                    scenario_name=sc.name,
                    solver_name=s_name,
                    status=SolveStatus.ERROR,
                    elapsed_sec=round(elapsed, 3),
                    hard_score=-999,
                    medium_score=0.0,
                    soft_score=0.0,
                    is_feasible=False,
                    blocker_count=1,
                    warning_count=0,
                    shortfall_slots=0,
                    stats={"error": str(exc)},
                )
            results.append(b_res)

    return results


def format_benchmark_report(results: Sequence[BenchmarkResult]) -> str:
    """ベンチマーク結果を Markdown 表形式に整形する。"""
    lines = [
        "# 📊 NuShift Benchmark Report (Timefold Toolkit 思想)",
        "",
        "| シナリオ | ソルバー | ステータス | 求解時間(s) | Hard スコア | Medium スコア | Soft スコア | 適法? | BLOCKER | 不足スロット |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for r in results:
        feas_str = "✅ YES" if r.is_feasible else "❌ NO"
        lines.append(
            f"| {r.scenario_name} | `{r.solver_name}` | {r.status.value if hasattr(r.status, 'value') else r.status} | "
            f"{r.elapsed_sec:.2f}s | {r.hard_score} | {r.medium_score} | {r.soft_score} | {feas_str} | "
            f"{r.blocker_count} | {r.shortfall_slots} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="NuShift Benchmark Toolkit")
    parser.add_argument(
        "--quick", action="store_true", default=False, help="高速サニティチェック実行"
    )
    parser.add_argument("--timeout", type=int, default=10, help="各求解の制限時間（秒）")
    parser.add_argument(
        "--solvers", nargs="+", default=["auto", "greedy"], help="対象ソルバーリスト"
    )
    args = parser.parse_args()

    print(
        f"🚀 ベンチマーク開始 (quick={args.quick}, timeout={args.timeout}s, solvers={args.solvers})..."
    )
    scenarios = get_benchmark_corpus(quick=args.quick)
    results = run_benchmark(scenarios, solvers=args.solvers, timeout_sec=args.timeout)
    report = format_benchmark_report(results)
    print("\n" + report)


if __name__ == "__main__":
    main()
