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

import ast
import inspect
import pathlib
import textwrap
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



# ---------------------------------------------------------------------------
# CBC を起動するテストの自動マーク（T-06）
# ---------------------------------------------------------------------------

CBC_ENTRYPOINTS = frozenset({"solve_shift", "solve_shift_greedy", "check_violations"})
"""CBC（MILP ソルバ）を起動しうる関数名。"""


def _called_names(node: ast.AST) -> set[str]:
    """AST 内で呼び出されている関数名をすべて集める。"""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Attribute):
                found.add(child.func.attr)
            elif isinstance(child.func, ast.Name):
                found.add(child.func.id)
    return found


def _fixture_uses_cbc() -> set[str]:
    """CBC を起動する（あるいは thereof に依存する）フィクスチャ名を集める。"""
    here = pathlib.Path(__file__).resolve()
    by_name: dict[str, ast.FunctionDef] = {}
    for candidate in sorted(here.parent.glob("conftest*.py")):
        tree = ast.parse(candidate.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                by_name[node.name] = node

    def resolve(name: str, seen: frozenset[str]) -> bool:
        if name in seen or name not in by_name:
            return False
        node = by_name[name]
        if _called_names(node) & CBC_ENTRYPOINTS:
            return True
        args = [a.arg for a in node.args.args]
        return any(resolve(a, seen | {name}) for a in args)

    return {name for name in by_name if resolve(name, frozenset())}


def _cbc_fixtures() -> set[str]:
    global _CBC_FIXTURES
    if _CBC_FIXTURES is None:
        _CBC_FIXTURES = _fixture_uses_cbc()
    return _CBC_FIXTURES


_CBC_FIXTURES: set[str] | None = None


CLI_ENTRYPOINTS = frozenset({"main", "_cli", "cmd_solve"})
"""CLI を起動する関数名。``shiftai.__main__.main`` とテスト内のラッパ。"""


def _calls_cli_solve(node: ast.AST) -> bool:
    """CLI の ``solve`` サブコマンドを呼んでいないか（CBC を起動する）。

    3つの形に対応する。

    * ``main(["solve", ...])`` — リストリテラルの第 1 要素
    * ``_cli("solve", ...)`` — 第 1 引数が文字列定数の ``solve``
    * ``main(build("out", []))`` — 第 1 引数がリテラルでないため、
      保守的に CBC を起動するとみなす（``presets`` / ``sample`` の呼び出しは
      リストリテラルなので誤検出しない）
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Attribute):
            name = child.func.attr
        elif isinstance(child.func, ast.Name):
            name = child.func.id
        else:
            continue
        if name not in CLI_ENTRYPOINTS or not child.args:
            continue
        first = child.args[0]
        if isinstance(first, ast.Constant):
            # _cli("solve", ...)
            if first.value == "solve":
                return True
            continue
        elements = getattr(first, "elts", None)
        if elements is None:
            # リテラルでない引数は conservatively に CBC とみなす
            return True
        if any(isinstance(el, ast.Constant) and el.value == "solve" for el in elements):
            return True
    return False


def _item_uses_cbc(item: pytest.Item) -> bool:
    """収集済みのテストが CBC を起動しうるか。

    呼び出しの直接検出に加えて、(1) CBC を使うフィクスチャを要求している場合、
    (2) CLI の ``solve`` サブコマンドを呼んでいる場合も対象とする。
    """
    func = getattr(item, "function", None)
    if func is None:
        return False
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    except (OSError, TypeError, SyntaxError):
        return False
    if _called_names(tree) & CBC_ENTRYPOINTS or _calls_cli_solve(tree):
        return True
    cbc_fixtures = _cbc_fixtures()
    return any(
        arg in cbc_fixtures
        for arg in inspect.signature(func).parameters
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """CBC を起動するテストに ``slow`` を自動的に付ける。

    ``make test-fast`` は ``-m "not slow"`` でCBC を起動するテストを除外する。
    手動の ``@pytest.mark.slow`` への依存をなくし、開始時点で対象が漏れないようにする。
    検証は ``tests/test_static_guards.py::test_slow未付与のCBCテストは存在しない`` で行う。
    """
    for item in items:
        if "slow" in item.keywords:
            continue
        if _item_uses_cbc(item):
            item.add_marker(pytest.mark.slow)


__all__ = [
    "DAY",
    "DAY_CLOSE",
    "DAY_OPEN",
    "FQ_KEY",
    "STANDARD_KEY",
    "WEEK_END",
    "WEEK_START",
    "CellState",
    "SolveResult",
    "part_contract",
    "sei_contract",
]
