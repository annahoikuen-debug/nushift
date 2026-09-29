"""テスト共通フィクスチャ。

方針:

* 職員は ``sample_data.make_staff()``（28 名）ではなく **6 名の小さなプール**を自前で
  組み立てる。ソルバ 1 回に 10 秒程度かかるため、職員数・時間帯数を絞り込むことで
  スイート全体を 1〜2 分に収める。
* 園児は 0 歳児 3 名 + 1 歳児 3 名（計 6 名）の 1 日分。seed 固定・決定論的。
* ``solved_day`` / ``solved_week`` は **セッションスコープ**で 1 回だけソルバを走らせ、
  gap_analysis / exporter / e2e のテストが同じ解を共有する。
"""

from __future__ import annotations

import warnings
from datetime import date, time

import pytest

from shiftai import local_rules, sample_data
from shiftai.domain import (
    AgeClass,
    CellState,
    ChildPlan,
    Contract,
    EmploymentType,
    FacilitySettings,
    Role,
    Slot,
    StaffMember,
    StaffPreferences,
    Unavailability,
    daterange,
)
from shiftai.gap_analysis import GapReport, analyze_gap
from shiftai.solver import SolveResult, solve_shift
from shiftai.standards import build_requirements

warnings.filterwarnings("ignore", category=DeprecationWarning)

DAY = date(2026, 9, 28)
WEEK_START = date(2026, 9, 28)
WEEK_END = date(2026, 10, 4)
DAY_OPEN = time(9, 0)
DAY_CLOSE = time(14, 0)

STANDARD_KEY = "全国基準（厚労省）"
FQ_KEY = "福岡市"

TIMEOUT = pytest.mark.timeout(300)


def sei_contract(**overrides) -> Contract:
    """週40h/日8h・8:30〜20:00 が出せる正職員契約。"""
    base = dict(
        weekly_hours=40.0,
        daily_hours=8.0,
        employment_type=EmploymentType.SEI,
        min_monthly_hours=60.0,
        max_monthly_hours=180.0,
        max_weekly_days=5,
        max_consecutive_days=5,
        min_rest_hours=11.0,
        granularity_min=30,
        earliest_start=time(7, 0),
        latest_end=time(20, 0),
        can_work_holiday=True,
        overtime_allowed=True,
    )
    base.update(overrides)
    return Contract(**base)


def part_contract(**overrides) -> Contract:
    """週20h/日5h のパート契約。"""
    base = dict(
        weekly_hours=20.0,
        daily_hours=5.0,
        employment_type=EmploymentType.PART,
        min_monthly_hours=0.0,
        max_monthly_hours=80.0,
        max_weekly_days=4,
        max_consecutive_days=5,
        min_rest_hours=11.0,
        granularity_min=30,
        earliest_start=time(7, 0),
        latest_end=time(20, 0),
    )
    base.update(overrides)
    return Contract(**base)


@pytest.fixture(scope="session")
def facility() -> FacilitySettings:
    """既定の園設定。"""
    return FacilitySettings()


@pytest.fixture(scope="session")
def one_day() -> list[date]:
    """1 日だけの対象日。"""
    return [DAY]


@pytest.fixture(scope="session")
def week_days() -> list[date]:
    """2026-09-28(月) 〜 2026-10-04(日) の 7 日。"""
    return list(daterange(WEEK_START, WEEK_END))


@pytest.fixture(scope="session")
def standard() -> object:
    """全国基準（厚労省）。"""
    return local_rules.get_standard(STANDARD_KEY)


@pytest.fixture(scope="session")
def fq_standard() -> object:
    """福岡市（延長保育の代替措置が使える基準）。"""
    return local_rules.get_standard(FQ_KEY)


@pytest.fixture(scope="session")
def small_children(one_day: list[date]) -> list[ChildPlan]:
    """0 歳児 3 名 + 1 歳児 3 名。決定論的に組み立てる。"""
    day = one_day[0]
    return [
        ChildPlan("C001", "園児001", day, AgeClass.INFANT, time(9, 0), time(13, 30)),
        ChildPlan("C002", "園児002", day, AgeClass.INFANT, time(9, 0), time(13, 30)),
        ChildPlan("C003", "園児003", day, AgeClass.INFANT, time(9, 0), time(13, 30)),
        ChildPlan("C004", "園児004", day, AgeClass.AGE_1, time(9, 0), time(13, 30)),
        ChildPlan("C005", "園児005", day, AgeClass.AGE_1, time(9, 0), time(13, 30)),
        ChildPlan("C006", "園児006", day, AgeClass.AGE_1, time(9, 0), time(13, 30)),
    ]


@pytest.fixture(scope="session")
def small_staff() -> list[StaffMember]:
    """保育士 4 名 + 子育て支援員 2 名 = 6 名。"""
    members: list[StaffMember] = []
    for index in range(4):
        members.append(
            StaffMember(
                staff_id=f"S{index + 1:03d}",
                name=f"保育士{index + 1}",
                roles=(Role.HOIKUSHI,),
                contract=sei_contract(),
                skills=frozenset({"乳幼児研修修了"}),
            )
        )
    for index in range(2):
        members.append(
            StaffMember(
                staff_id=f"S{index + 5:03d}",
                name=f"支援員{index + 1}",
                roles=(Role.SHIENSHIIN,),
                contract=part_contract(),
            )
        )
    return members


@pytest.fixture(scope="session")
def small_preferences(small_staff, one_day) -> dict[str, StaffPreferences]:
    """S006 を丸 1 日休ませる。"""
    day = one_day[0]
    prefs: dict[str, StaffPreferences] = {m.staff_id: StaffPreferences() for m in small_staff}
    prefs["S006"] = StaffPreferences(
        unavailable=[
            Unavailability(day=day, start=time(0, 0), end=time(23, 59), reason="希望休")
        ]
    )
    return prefs


@pytest.fixture(scope="session")
def small_requirements(small_children, one_day, standard) -> object:
    """小さな問題の必要人員表（全国基準・1 日）。"""
    return build_requirements(
        small_children,
        one_day,
        standard,
        day_open=DAY_OPEN,
        day_close=DAY_CLOSE,
        granularity_min=30,
    )


@pytest.fixture(scope="session")
def dataset(one_day):
    """サンプルデータ（1 日分・seed=42）。"""
    return sample_data.make_dataset(one_day, seed=42)


@pytest.fixture(scope="session")
def slots(small_requirements) -> tuple[Slot, ...]:
    """小さな問題の時間帯（10 個）。"""
    return tuple(small_requirements.slots)


@pytest.fixture(scope="session")
def solved_day(small_children, small_staff, small_preferences, small_requirements):
    """1 日・6 名で解いた結果。MILP が 1 回だけ走る。"""
    return solve_shift(
        small_children,
        small_staff,
        small_requirements,
        small_preferences,
        time_limit_sec=25,
        standard=local_rules.get_standard(STANDARD_KEY),
    )


@pytest.fixture(scope="session")
def day_report(small_requirements, solved_day, small_staff) -> GapReport:
    """``solved_day`` の過不足レポート。"""
    return analyze_gap(small_requirements, solved_day, small_staff)


@pytest.fixture(scope="session")
def week_inputs(week_days) -> tuple[list, list, dict, object]:
    """1 週間分のサンプルデータと必要人員表（福岡市基準）。"""
    children, staff, prefs = sample_data.make_dataset(week_days, seed=42)
    std = local_rules.get_standard(FQ_KEY)
    table = build_requirements(
        children,
        week_days,
        std,
        day_open=time(7, 15),
        day_close=time(19, 30),
        granularity_min=30,
    )
    return children, staff, prefs, table


@pytest.fixture(scope="session")
def solved_week(week_inputs):
    """1 週間サンプルを実際に最適化した結果（slow テスト_shared）。"""
    children, staff, prefs, table = week_inputs
    return solve_shift(
        children,
        staff,
        table,
        prefs,
        time_limit_sec=180,
        standard=local_rules.get_standard(FQ_KEY),
    )


__all__ = [
    "DAY",
    "DAY_CLOSE",
    "DAY_OPEN",
    "FQ_KEY",
    "STANDARD_KEY",
    "TIMEOUT",
    "WEEK_END",
    "WEEK_START",
    "CellState",
    "SolveResult",
    "part_contract",
    "sei_contract",
]
