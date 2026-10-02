"""``make test-fast`` が本当に CBC を起動しないことの検証（R1-F）。

**何が問題だったか**

``tests/conftest.py`` の ``slow`` 自動付与は **AST 静的解析** ベースで、
テスト本体に ``solve_shift`` が直接現れることだけをDetectしていた。
そのため以下の経路が漏れて ``-m "not slow"`` でも CBC が起動していた:

* モジュールレベルのヘルパー経由（``test_solver.py`` の ``_solve``）
* モジュールレベルの ``@pytest.fixture`` 経由（``test_cost_basis.py`` の ``solved``）
* 生 PuLP（``ctx.prob.solve(pulp.PULP_CBC_CMD(...))``）
* ``shiftai.solver._run_cbc`` の直接 import・呼び出し

**実測**: 修正前は ``-m "not slow"`` の実行中に CBC が **178 回** 起動していた。

**このファイルのアプローチ**

静的解析では「漏れ'absence」を証明できないので、
**CBC を起動する関数に計数器を埋め込み、
``pytest -m "not slow"`` をサブプロセスで実際に走らせて回数を突き合わせる**。
これで「高速モードに CBC が混ざっていない」ことを実行で保証する。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: CBC 起動の回数を記録するログのパス（サブプロセスに環境変数で渡す）。
CBC_LOG_ENV = "SHIFTAI_CBC_CALLS_LOG"

#: ``pytest --collect-only -q`` が deselect 件数を書くときの書式。
#: 実際に観測される 2 形態を受け付ける:
#:     ``23/24 tests collected (1 deselected) in 1.37s``
#:     ``23/24 tests collected (1 deselected)``
#: ``23``（収集数）を ``1``（除外数）と取り違えないよう、**必ず「deselected」表記の
#: 直前の整数**を拾う。``N/M`` の N を取ってはいけない。
_DESELECTED_RE = re.compile(r"\(?(\d+)\s+deselected")


def _parse_deselected(stdout: str) -> int | None:
    """pytest の収集サマリーから「除外件数」を取り出す。

    書式が変わって取り出せなかったときは ``None`` を返す（``0`` ではない）。
    ``0`` を返すと「除外 0 件（＝条件を満たす）」と取り違えて、
    テストが常に通ってしまう（``docs/09`` R1-E「空回りするアサーション」と同じ型）。

    :returns: 除外件数。サマリー行が見つからなければ ``None``
    """
    for line in stdout.splitlines():
        m = _DESELECTED_RE.search(line)
        if m:
            return int(m.group(1))
    return None


def _run_test_fast(tmp_path: Path) -> tuple[int, int]:
    """``pytest -m "not slow"`` を CBC 計数付きで走らせ、``(rc, 起動回数)`` を返す。

    計数器は ``shiftai.solver`` の CBC 実行部に一時的に注入する。
    ``conftest.py`` は汚さない（サブプロセス側で injection する）。
    """
    log = tmp_path / "cbc_calls.log"
    # ``PYTEST_PLUGINS`` はモジュール名を受け取るので、
    # プラグインを ``tmp_path`` に置いて ``PYTHONPATH`` 名前空間に入れる。
    plugin = tmp_path / "cbc_counter_plugin.py"
    plugin.write_text(
        "\n".join(
            [
                "import os",
                "import pulp",
                "",
                "_LOG = os.environ.get('" + CBC_LOG_ENV + "')",
                "_real = pulp.PULP_CBC_CMD",
                "",
                "def _counted(*a, **kw):",
                "    if _LOG:",
                "        with open(_LOG, 'a', encoding='utf-8') as fh:",
                "            fh.write('x\\n')",
                "    return _real(*a, **kw)",
                "",
                "pulp.PULP_CBC_CMD = _counted",
            ]
        ),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env[CBC_LOG_ENV] = str(log)
    env["PYTEST_PLUGINS"] = "cbc_counter_plugin"
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "not slow",
            "-q",
            "--timeout=900",
            "-p",
            "no:cacheprovider",
            "--no-header",
            "-x",
            "--ignore",
            str(ROOT / "tests" / "test_slow_marker.py"),
        ],
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=3000,
        env=env,
    )
    calls = len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0
    return proc.returncode, calls


@pytest.mark.slow
def test_高速モードはCBCを起動しない(tmp_path: Path) -> None:
    """``-m "not slow"`` の実行中に CBC（MILP ソルバ）が 1 度も起動しないこと。

    ``make test-fast`` が「軽い統合テストだけ」を実行するという約束を、
    実行によって検証する。静的な ``slow`` 付与の検証
    （``test_static_guards.py::test_slow未付与のCBCテストは存在しない``）とは
    別の角度から同じ性質を保証する。
    """
    rc, calls = _run_test_fast(tmp_path)
    assert calls == 0, (
        f'-m "not slow" の実行中に CBC が {calls} 回起動した\n'
        "（tests/conftest.py の slow 自動付与が漏れている）"
    )
    assert rc == 0, "高速モードのテストが失敗している"


def test_slow自動付与はモジュールレベルヘルパーを辿る() -> None:
    """``_solve(...)`` のようなモジュールヘルパー経由でも slow が付くこと。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "shiftai_tests_conftest", ROOT / "tests" / "conftest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # キーは **(モジュール名, 関数名)**。名前だけで引くと、
    # 別モジュールの同名ヘルパーが巻き添えになる（``run`` が実際にその例）。
    helpers = module._helper_uses_cbc()
    assert helpers, "ヘルパーテーブルが空"
    for mod, name in (
        ("test_solver.py", "_solve"),
        ("test_gap_analysis.py", "_greedy"),
    ):
        assert helpers.get((mod, name)) is True, (
            f"モジュールレベルヘルパー {mod}::{name} が CBC 起動として検出されていない"
        )

    # 入れ子関数はモジュールレベルの補助関数ではないため、
    # テーブルに入っていない（``run`` が実害の原因だった）
    assert ("test_cli_exit_codes.py", "run") not in helpers, (
        "テスト関数の中にある入れ子ヘルパーがテーブルに混入している"
        "（名前衝突で無関係なテストが slow になる）"
    )

    fixtures = module._cbc_fixtures()
    for name in ("solved", "solved_day", "solved_week"):
        assert name in fixtures, f"フィクスチャ {name} が CBC 使用として検出されていない"


def test_slow自動付与は生PuLPと_run_cbcを検出する() -> None:
    """``PULP_CBC_CMD`` 直呼びと ``_run_cbc`` import 経路も検出すること。"""
    import ast
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "shiftai_tests_conftest2", ROOT / "tests" / "conftest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # 生 PuLP
    raw = ast.parse("ctx.prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=20))")
    assert module._raw_cbc_calls(raw), "PULP_CBC_CMD 直呼びが検出されない"

    # _run_cbc の import
    imported = ast.parse(
        "from shiftai.solver import _run_cbc\nstatus, _raw = _run_cbc(ctx, 5, False)\n"
    )
    assert module._called_names(imported) & module._ALL_CBC_CALLEES, (
        "_run_cbc が CBC エントリポイントとして認識されていない"
    )


def _make_sourceless():
    """``inspect.getsource`` が ``OSError`` になる関数を作る。

    ``exec`` で作った関数にはソースコードが無いため、
    ``inspect.getsource`` が失敗する（デコレータや ``exec`` で
    生成されたテストの場合に相当する）。
    """
    namespace: dict[str, object] = {}
    exec("def generated():\n    return 1\n", namespace)  # noqa: S102
    return namespace["generated"]


def test_ソースが取れないテストはslow扱いになる() -> None:
    """``inspect.getsource`` が失敗しても「CBC を起動しない」とは断定しない。

    修正前:  introspection 失敗時に ``return False`` していたため、
    デコレータで隠されたテストが「CBC を起動しない」と誤認され、
    ``-m "not slow"`` で実行されていた。
    """

    class _NoSource:
        pass

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "shiftai_tests_conftest3", ROOT / "tests" / "conftest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fake_item = type("Item", (), {"function": staticmethod(_make_sourceless())})()
    assert module._item_uses_cbc(fake_item) is True, (
        "ソースが取れないテストを「CBC を起動しない」と判定している"
    )


def test_testfastの対象が実際に多い() -> None:
    """``-m "not slow"`` が実行対象を余分に除外していないこと。

    全部を slow にしてしまう実装でも上の CBC カウンタは 0 になるため、
    「CBC を使わないテストが残っている」ことも併せて主張する。
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "not slow",
            "--collect-only",
            "-q",
            "--timeout=300",
            "-p",
            "no:cacheprovider",
            "--no-header",
        ],
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
    )
    assert proc.returncode == 0, proc.stdout[-2000:]
    collected = len(
        [ln for ln in proc.stdout.splitlines() if "::" in ln and ln.startswith("tests/")]
    )
    assert collected > 500, (
        f'-m "not slow" で収集できたテストが {collected} 件しかない'
        "（全部 slow にして_fast が空になっている）"
    )


def test_CBCを起動しない検査はslowにしない() -> None:
    """``check_violations`` を使うだけのテストを ``slow`` にしないこと。

    ``check_violations`` は GapReport を組み立てるだけの純粋な Python 関数で、
    ``pulp`` も ``.solve()`` も参照しない（実測済み）。
    これを CBC エントリポイントに含めると、
    ``tests/test_weekly_normalization.py`` のように
    **「CBC を起動しないことが設計上のゴール」のファイルが丸ごと
    ``test-fast`` から除外される**（過マーク）。
    Round 2 の実測で 12 テストが過マークされていた。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "shiftai_conftest_nocbc", ROOT / "tests" / "conftest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert "check_violations" not in module.CBC_ENTRYPOINTS, (
        "check_violations が CBC エントリポイントに入っている（CBC を起動しないのに slow になる）"
    )

    # weekly_normalization はほぼ全体が test-fast に残るべき
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "not slow",
            "--collect-only",
            "-q",
            "--timeout=300",
            "-p",
            "no:cacheprovider",
            "--no-header",
            "tests/test_weekly_normalization.py",
        ],
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
    )
    assert proc.returncode == 0, proc.stdout[-2000:]
    # ``pytest --collect-only -q`` の末尾は
    #   ``N/M tests collected (K deselected) in Xs``
    # という形式。``K`` が ``test-fast`` から除外された件数。
    # サマリーを解釈できなかったときは「0 件」と決めつけない（=常時合格にしない）。
    deselected = _parse_deselected(proc.stdout)
    assert deselected is not None, (
        "pytest の収集サマリーから deselected 件数を読めなかった\n"
        f"--- stdout ---\n{proc.stdout[-2000:]}"
    )
    assert deselected <= 2, (
        f"test_weekly_normalization.py で {deselected} 件が test-fast から除外"
        "（このファイルは CBC を起動しないことがゴール）"
    )


# --------------------------------------------------------------------------
# サマリーパーサ自体のテスト
#
# このパーサは「pytest の出力を数える」ため、**数え間違いしてもテストは通る**。
# 実際に一度壊れた（前版は ``N/M tests collected (K deselected)`` から
# ``N``=収集数を ``K`` と誤読して 23 件と報告し、無関係に落ちた）。
# パーサ単体を固定しておかないと、同じ型の欠陥が再発しても検出できない。
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        # 実際に観測される書式（収集数 23 / 除外数 1）
        ("23/24 tests collected (1 deselected) in 1.37s", 1),
        ("23/24 tests collected (1 deselected)", 1),
        # 除外 0 件（括弧つき）
        ("902/1153 tests collected (251 deselected) in 3.28s", 251),
        ("24/24 tests collected in 1.37s", None),
        # 複数桁
        ("1/2 tests collected (1 deselected)", 1),
    ],
)
def test_収集サマリーの除外件数を正しく読む(stdout: str, expected: int | None) -> None:
    """``N/M tests collected (K deselected)`` から **K** を取り出すこと。

    前版は ``N``（収集数）を返していたため、1 件だけ除外されている実情を
    「23 件除外」と誤って報告し、このテストが落ちていた。
    """
    assert _parse_deselected(stdout) == expected


def test_サマリーが無ければ0ではなくNoneを返す() -> None:
    """書式が変わっても「0 件＝条件を満たす」で常時合格にしないこと。"""
    assert _parse_deselected("") is None
    assert _parse_deselected("no tests ran\n") is None


# ---------------------------------------------------------------------------
# R3-QA-01: ヘルパー名衝突による過マーク（UI 68 件の消失）
# ---------------------------------------------------------------------------


def test_別モジュールの同名ヘルパーがslowを伝播させない() -> None:
    """``run`` のような一般的な名前が他モジュールに漏れないこと。

    Round 3 の実測: ``tests/test_cli_exit_codes.py`` にある入れ子の
    ``def run(...)``（CLI の ``solve`` を呼ぶ）が ``helpers["run"] = True`` を
    作り、名前だけで引いていたため、Streamlit の ``AppTest.run()`` を
    呼ぶ **7 モジュール計 68 件の UI テストがすべて slow 扱い**になり、
    ``make test-fast`` から消えていた。

    → ヘルパーテーブルは **(モジュール名, 関数名)** でスコープされていること。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "shiftai_conftest_scope", ROOT / "tests" / "conftest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    helpers = module._helper_uses_cbc()
    assert helpers, "ヘルパーテーブルが空"
    for key in helpers:
        assert isinstance(key, tuple) and len(key) == 2, (
            f"キーが (モジュール名, 関数名) になっていない: {key!r}"
        )


@pytest.mark.parametrize(
    ("module_name", "expected_excluded"),
    [
        # AppTest のみで CBC を起動しないモジュール → test-fast に全て残る
        # （Round 3 実測では、ここが 20 件 / 2 件 過マークされていた）
        ("test_ui_sidebar.py", 0),
        ("test_theme.py", 0),
        ("test_ui_state.py", 0),
        # `violations` / `day_shift` が `solved_day`（実 CBC）に依存するため、
        # このモジュールの 18 件は正当に slow。
        ("test_ui_components.py", 18),
    ],
)
def test_CBCを使わないUIモジュールはtest_fastに残る(
    module_name: str, expected_excluded: int
) -> None:
    """AppTest だけのモジュールが ``slow`` にされていないこと。

    ``test_testfastの対象が十分に多い`` の総数閾値では
    「1 モジュール分のテストが丸ごと消えた」ことを検出できない。
    総数ではなく**対象モジュール単位**で確認する。
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "not slow",
            "--collect-only",
            "-q",
            "--timeout=300",
            "-p",
            "no:cacheprovider",
            "--no-header",
            f"tests/{module_name}",
        ],
        cwd=str(ROOT),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
    )
    assert proc.returncode == 0, proc.stdout[-2000:]
    # pytest は「除外 0 件」のときはサマリに ``(K deselected)`` を出さない。
    # したがって「行が無い = 0 件」とみなすのは避け、
    # 除外件数を読める行が存在すること、または総数がちょうど
    # 対象件数であることを**必ず**確認する。
    m = re.search(r"\((\d+)\s+deselected\)", proc.stdout)
    if m:
        excluded = int(m.group(1))
    else:
        # サマリに無い = 除外 0 件。収集件数が総数と一致することを代替証拠にする。
        summary = [
            ln for ln in proc.stdout.splitlines() if "collected" in ln or "no tests ran" in ln
        ]
        assert summary, (
            f"収集サマリも無い（pytest の出力形式が変わった可能性）:\n{proc.stdout[-2000:]}"
        )
        excluded = 0
    assert excluded == expected_excluded, (
        f"{module_name} の {excluded} 件のテストが test-fast から除外されている"
        f"（期待 {expected_excluded}）"
    )


def test_入れ子ヘルパーが判定に漏れない() -> None:
    """テスト関数の中の入れ子 ``def`` はテーブルに入らないこと。

    モジュールレベルの補助関数だけが「CBC を起動するヘルパー」であり、
    入れ子の ``def run(...)`` を拾うと
    ``AppTest.run()`` が巻きこされる。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "shiftai_conftest_nested", ROOT / "tests" / "conftest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    helpers = module._helper_uses_cbc()
    assert ("test_cli_exit_codes.py", "run") not in helpers, (
        "入れ子ヘルパーがテーブルに入っている（名前衝突で無関係なテストが slow になる）"
    )
