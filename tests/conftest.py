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
import re
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
        unavailable=[Unavailability(day=day, start=time(0, 0), end=time(23, 59), reason="希望休")]
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


CBC_ENTRYPOINTS = frozenset({"solve_shift", "solve_shift_greedy"})
"""CBC（MILP ソルバ）を起動する関数名。

``check_violations`` は **ここに含めない**。
GapReport を組み立てるだけの純粋な Python 関数で、
``pulp`` も ``.solve()`` も参照しない（実測済み）。
含めると ``tests/test_weekly_normalization.py`` のように
「CBC を起動しないことが設計上のゴール」のファイルが
丸ごと ``test-fast`` から除外されてしまう（過マーク）。
"""

#: ``shiftai.solver`` から import して直接呼ぶ CBC 起動関数。
#: テストが ``_run_cbc(ctx, 5, False)`` と呼ぶと、この関数が
#: ``ctx.prob.solve(pulp.PULP_CBC_CMD(...))`` を実行する。
#: ``solve_shift`` を通らないので :data:`CBC_ENTRYPOINTS` では検出できない。
SOLVER_CBC_ENTRYPOINTS = frozenset({"_run_cbc"})
"""ソルバ内部の CBC 起動関数（import して直接呼ぶ）。"""

#: ``pulp`` を直接触って CBC を起動する呼び出し。
#: ``solve_shift`` を経由せずに ``ctx.prob.solve(pulp.PULP_CBC_CMD(...))`` と
#: 呼ぶテストは、上のエントリポイント名では **検出できない**。
RAW_CBC_MARKERS = frozenset({"PULP_CBC_CMD", "COIN_CMD", "GLPK_CMD", "CBC_CMD"})
"""生 PuLP で CBC を起動する関数名。"""

_ALL_CBC_CALLEES = CBC_ENTRYPOINTS | SOLVER_CBC_ENTRYPOINTS
"""CBC を起動しうる関数名の総集。"""


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


def _raw_cbc_calls(node: ast.AST) -> bool:
    """``PULP_CBC_CMD`` などを直接起動していないか。

    ``solve_shift`` を経由しない生 PuLP 経路
    （``ctx.prob.solve(pulp.PULP_CBC_CMD(timeLimit=20))``）は
    :data:`CBC_ENTRYPOINTS` では検出できないため、別途見る。
    """
    return (
        bool(_called_names(node) & RAW_CBC_MARKERS)
        or bool(re.search(r"\bPULP_CBC_CMD\b|\bCOIN_CMD\b", ast.dump(node)))
        or any(
            isinstance(child, ast.Attribute) and child.attr == "solve" for child in ast.walk(node)
        )
    )


def _module_functions_that_use_cbc() -> dict[str, bool]:
    """各テストモジュール内の関数について「CBC を起動しうるか」を返す。

    ``tests/test_solver.py`` の ``_solve`` や ``tests/test_solver_perf.py`` の
    ``_solve`` のように、**モジュールレベルのヘルパーを経由する**と
    テスト本体には ``solve_shift`` が現れないため、
    テスト本体の AST だけでは検出できない。

    返り値は **(モジュール名, 関数名) -> CBC を起動しうるか**。

    .. warning::
       **キーを「関数名」だけにすると衝突する。**
       実際、``tests/test_cli_exit_codes.py`` にある入れ子の ``def run(...)``
       （CLI の ``solve`` を呼ぶ）は ``helpers["run"] = True`` を-rules、
       それを**名前だけで**引くため、Streamlit の ``AppTest.run()`` を
       呼ぶ 7 モジュールの UI テスト（計 68 件）すべてが ``slow`` 扱いになり、
       ``make test-fast`` から消えていた。
       そのため **モジュール単位でスコープし、
       定義しているモジュールが同じ場合だけ** 参照する。
       また入れ子関数（``def run`` がテスト関数の中にあるケース）は
       モジュールレベルの補助関数ではないため、判定対象に含めない。
    """
    result: dict[tuple[str, str], bool] = {}
    for module in sorted(pathlib.Path(__file__).resolve().parent.glob("test_*.py")):
        try:
            tree = ast.parse(module.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - 壊れたファイル
            continue
        # **モジュールレベル**の関数だけを集める（入れ子を除外する）
        nodes = {
            n.name: n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        }

        def uses_cbc(
            node: ast.FunctionDef,
            seen: frozenset[str],
            table: dict[str, ast.FunctionDef] = nodes,
        ) -> bool:
            if node.name in seen:
                return False
            if _called_names(node) & _ALL_CBC_CALLEES or _raw_cbc_calls(node):
                return True
            if _calls_cli_solve(node):
                return True
            for callee in _called_names(node):
                if callee.startswith("test_") or callee not in table:
                    continue
                if uses_cbc(table[callee], seen | {node.name}):
                    return True
            return False

        for name, node in nodes.items():
            result.setdefault((module.name, name), uses_cbc(node, frozenset()))
    return result


_HELPER_USES_CBC: dict[tuple[str, str], bool] | None = None


def _helper_uses_cbc() -> dict[tuple[str, str], bool]:
    global _HELPER_USES_CBC
    if _HELPER_USES_CBC is None:
        _HELPER_USES_CBC = _module_functions_that_use_cbc()
    return _HELPER_USES_CBC


def _is_fixture(node: ast.FunctionDef) -> bool:
    """``@pytest.fixture`` が付いている関数か。"""
    for deco in node.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        name = getattr(target, "attr", None) or getattr(target, "id", None)
        if name == "fixture":
            return True
    return False


def _fixture_uses_cbc() -> set[str]:
    """CBC を起動する（あるいは thereof に依存する）フィクスチャ名を集める。

    ``conftest*.py`` だけでなく **全テストモジュール** の
    モジュールレベル ``@pytest.fixture`` も対象にする。
    ``tests/test_cost_basis.py`` の ``solved``（``scope="module"``）のように
    テスト本体のローカルにないフィクスチャが CBC を起動するため、
    ここに載せないと「テスト本体に CBC 呼び出しが無い」ので
    slow が付かないまま実行されてしまう。

    テスト関数そのもの（``test_*``）はフィクスチャではないので集めない。
    """
    here = pathlib.Path(__file__).resolve()
    modules = [here, *sorted(here.parent.glob("test_*.py"))]
    by_name: dict[str, ast.FunctionDef] = {}
    for candidate in modules:
        try:
            tree = ast.parse(candidate.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - 壊れたファイル
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and _is_fixture(node):
                by_name.setdefault(node.name, node)

    def resolve(name: str, seen: frozenset[str]) -> bool:
        if name in seen or name not in by_name:
            return False
        node = by_name[name]
        if _called_names(node) & _ALL_CBC_CALLEES or _raw_cbc_calls(node):
            return True
        if _calls_cli_solve(node):
            return True
        helpers = _helper_uses_cbc()
        if any(n for n in _called_names(node) if not n.startswith("test_") and helpers.get(n)):
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


def _item_uses_cbc(item: pytest.Item) -> bool:
    """収集済みのテストが CBC を起動しうるか。

    呼び出しの直接検出に加えて、(1) CBC を使うフィクスチャを要求している場合、
    (2) CLI の ``solve`` サブコマンドを呼んでいる場合、
    (3) **モジュールレベルのヘルパー関数**（``_solve(...)`` など）を
    呼んでいる場合、

    のいずれも CBC を起動しうるものとして扱う。
    """
    func = getattr(item, "function", None)
    if func is None:
        return False
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    except (OSError, TypeError, SyntaxError):
        # ソースが取れないとき「CBC を起動しない」とは断定できない
        # （保守側に倒す：判断できないので slow にする）。
        return True
    if _called_names(tree) & _ALL_CBC_CALLEES or _raw_cbc_calls(tree) or _calls_cli_solve(tree):
        return True
    # テスト本体が**同じモジュール.define**のヘルパー（``_solve`` など）を
    # 呼んでいて、そのヘルパーが CBC を起動する場合。
    #
    # モジュールでスコープし直すのは、
    # 別モジュールの同名ヘルパー（``run`` など）が
    # ``AppTest.run()`` まで巻き添えに ``slow`` にしていたため。
    helpers = _helper_uses_cbc()
    mod_name = _module_name_of(item)
    for name in _called_names(tree):
        if name.startswith("test_"):
            continue
        # 属性呼び出し（``at.run()`` / ``proc.run()``）は、
        # そのモジュールが定義した関数ではない可能性が高い。
        # メソッド名とヘルパー名の偶然の一致で誤検出しないよう、
        # **同じモジュールでモジュールレベル定義されている場合だけ** 見る。
        if mod_name is not None and helpers.get((mod_name, name)):
            return True
    cbc_fixtures = _cbc_fixtures()
    return any(arg in cbc_fixtures for arg in inspect.signature(func).parameters)


def _module_name_of(item: pytest.Item) -> str | None:
    """テストが属するモジュールのファイル名（``test_ui_tabs.py`` 等）を返す。"""
    module = getattr(item, "module", None)
    path = getattr(module, "__file__", None)
    if path:
        return pathlib.Path(path).name
    name = getattr(module, "__name__", None)
    return name.rpartition(".")[2] if name else None


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
