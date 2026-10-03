"""公平性・即時検証・ラダーの回帰テスト（Round 4: R2-FV-*）。

**何をどこで壊していたか**

* **R2-FV-04 (P0)**: 微調整タブの「確定して再最適化する」ボタンに
  ``disabled`` がなく、**配置基準に抵触する変更でも確定できていた**。
  即時検証が赤で「このまま確定すると抵触します」と表示しているのに、
  ユーザーは押せてしまい、その結果が勤務表として公開されていた。
* **R2-FV-13 (P1)**: 緩和ラダーが ``normalize_level(None)``（= 0）を使い、
  **L0 の 1 段階しか試していなかった**（docstring は「最大段階まで」と書いている）。
* **R2-FV-14 (P1)**: ラダーが ``PARTIAL``（ベストエフォート＝、まさに目的の場合）
  を「解けなかった」と判定し、**人員不足ケースで必ず失敗と報告していた**。
* **R2-FV-03 (P2)**: 公平性の最小値に**配置対象外職員（園長など）**を含め、
  KPI が構造的に 0 に固定されていた。
"""

from __future__ import annotations

import inspect
from datetime import date, time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# R2-FV-04: 確定ボタンの無効化
# ---------------------------------------------------------------------------


def test_確定ボタンは検証エラーがあるとき無効になる() -> None:
    """微調整タブの確定ボタンが ``disabled=`` を検証結果に結び付けていること。

    ``tab_data.py`` は既に ``disabled=report.has_errors`` を使っていたが、
    ``tab_shift.py`` は対応していなかった（意図ではなく **対応漏れ**）。
    """
    from shiftai.ui import tab_shift

    source = inspect.getsource(tab_shift._render_editor)
    assert "live_report" in source, "検証レポートを受け取っていない"
    assert "disabled=has_errors" in source, (
        "確定ボタンが disabled で保護されていない（配置基準違反の変更まで確定できてしまう）"
    )
    assert "live_report.has_errors" in source, "disabled が検証結果に結び付いていない"


def test_検証できない場合は確定を止められる() -> None:
    """``live_report`` が ``None`` のとき ``has_errors`` が偽にならないこと。

    以前は ``live_report is not None and live_report.has_errors`` で
    短絡評価していたため、検証そのものが skipped された状態（必要人員が
    未計算）で **無検証のまま確定できる** 状態になっていた。
    fail-closed へ倒す。
    """
    from shiftai.ui import tab_shift

    source = inspect.getsource(tab_shift._render_editor)
    assert "live_report is None or live_report.has_errors" in source, (
        "検証不能時に確定を止められない（fail-open になっている）"
    )
    assert "live_report is not None" not in source, (
        "旧式の短絡評価が残っている（検証不能時に確定が通ってしまう）"
    )


def test_生きた検証はレポートを返す() -> None:
    """``_render_live_check`` が ``LiveReport`` を返すこと。"""
    from shiftai.ui import tab_shift

    source = inspect.getsource(tab_shift._render_live_check)
    assert re.search(r"^    return report\s*$", source, re.M), (
        "_render_live_check がレポートを返していない（呼び出し側が disabled に使える）"
    )

    """アプリが例外なく起動し、微調整タブの構成要素が揃うこと。

    「確定」ボタンの実挙動はデータが投入済みの状態に依存するため、
    ここでは **起動の健全性とタブ構成**を主張し、
    ``disabled`` の結び付けは上の構造検査テストが担保する。
    """
    pytest.importorskip("streamlit.testing.v1")
    app_test = pytest.importorskip("streamlit.testing.v1.app_test")

    at = app_test.AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=300).run()
    assert not [str(e.value) for e in at.exception], [str(e.value) for e in at.exception]
    assert at.tabs, "タブが 1 つも描画されていない"

    # 上級者モード（5 タブ）へ切り替えても例外が出ないこと
    at.toggle(key="ui_simple_mode").set_value(False).run()
    assert not [str(e.value) for e in at.exception], [str(e.value) for e in at.exception]
    assert len(at.tabs) == 5, f"上級者モードが {len(at.tabs)} タブ（5 を期待）"

    # _simple モードへ戻しても例外が出ない（トグルの往復）
    at.toggle(key="ui_simple_mode").set_value(True).run()
    assert not [str(e.value) for e in at.exception], [str(e.value) for e in at.exception]
    assert len(at.tabs) == 3, f"シンプルモードが {len(at.tabs)} タブ（3 を期待）"


# ---------------------------------------------------------------------------
# R2-FV-13 / R2-FV-14: 緩和ラダー
# ---------------------------------------------------------------------------


def test_ラダーの既定は最大段階まで試す() -> None:
    """``max_level=None`` のとき最大段階まで試すこと。

    修正前: ``normalize_level(None)`` は 0 を返すため L0 の 1 回しか試さず、
    docstring の「L0 から順に試す」と食い違っていた。
    """
    from shiftai import diagnostics
    from shiftai.relaxation import RELAX_LEVELS, RelaxLevel

    source = inspect.getsource(diagnostics.relaxation_ladder)
    assert "max_level is None" in source, "max_level=None を特別扱いしていない"
    assert "len(RELAX_LEVELS) - 1" in source, "既定の上限が最大段階になっていない（L0 のみ）"
    assert len(RELAX_LEVELS) - 1 == int(RelaxLevel.IGNORE_UNAVAILABLE)


def test_ベストエフォート解は解けたと判定する() -> None:
    """``PARTIAL``（ベストエフォート）を「解けた」と判定すること。

    ラダーが存在する意義は「ハード制約だと解けないが、
    緩和すると解ける」ケースの探索である。
    まさにその状況（配置基準に不足あり＝``PARTIAL``）を
    「解けなかった」と判定していたため、必ず失敗を報告していた。
    """
    from shiftai import diagnostics

    source = inspect.getsource(diagnostics.relaxation_ladder)
    assert "SolveStatus.PARTIAL" in source, "PARTIAL（ベストエフォート）を解けたと判定していない"
    # 3 つすべてが「解けた」側に含まれること
    seg = source[source.find("solved = ") : source.find("solved = ") + 320]
    for status in ("OPTIMAL", "FEASIBLE", "PARTIAL"):
        assert status in seg, f"{status} が判定に含まれていない"


def test_ラダーは段階ごとに結果を記録する() -> None:
    """各段階の結果が 1 件ずつ記録されること（重複・欠落がないこと）。"""
    from shiftai.diagnostics import LadderOutcome, RelaxationLadder
    from shiftai.domain import SolveStatus

    outcomes = (
        LadderOutcome(
            level=0,
            label="L0",
            status=SolveStatus.INFEASIBLE,
            solved=False,
            gap_slots=3,
            elapsed_sec=0.1,
        ),
        LadderOutcome(
            level=1,
            label="L1",
            status=SolveStatus.PARTIAL,
            solved=True,
            gap_slots=1,
            elapsed_sec=0.2,
        ),
    )
    ladder = RelaxationLadder(outcomes=outcomes)
    assert ladder.first_solved is not None
    assert ladder.first_solved.level == 1
    assert len(ladder.outcomes) == 2


# ---------------------------------------------------------------------------
# R2-FV-03: 公平性の最小値に園長を含めない
# ---------------------------------------------------------------------------


def test_配置対象外職員は公平性の最小値に含めない() -> None:
    """園長（``is_placeable`` が False）は公平性集計に含まれないこと。

    ソルバの公平性目的関数も配置対象職員のみを対象にしているため、
    UI だけが園長を含めると「最小値」が 0 に固定され、
    永远不会解消しない偏りを表示することになる。
    """
    import pandas as pd

    from shiftai import fairness, local_rules
    from shiftai.domain import (
        AgeClass,
        CellState,
        ChildPlan,
        EmploymentType,
        Role,
        ShiftAssignment,
        ShiftDay,
        SolveResult,
        SolveStatus,
        StaffMember,
    )
    from tests.conftest import STANDARD_KEY, part_contract

    day = date(2026, 9, 28)
    standard = local_rules.get_standard(STANDARD_KEY)
    slots = pd.DataFrame  # placeholder to keep import used

    hoikushi = StaffMember(
        "S001",
        "保育士A",
        (Role.HOIKUSHI,),
        part_contract(employment_type=EmploymentType.SEI),
    )
    # 園長は保育基準の配置対象ではない（``is_placeable`` が False）
    principal = StaffMember(
        "S900",
        "園長",
        (Role.ENJOGAKUIN,),
        part_contract(employment_type=EmploymentType.SEI),
    )

    from shiftai.domain import build_slots

    slot_list = build_slots(time(7, 0), time(18, 0), 60)
    early = [s for s in slot_list if s.start < time(9, 0)]

    result = SolveResult(
        status=SolveStatus.OPTIMAL,
        shift_days=[
            ShiftDay(
                day=day,
                assignments={"S001": {s.label: CellState.WORK for s in early}},
            )
        ],
        assignments=[ShiftAssignment("S001", day, s, CellState.WORK) for s in early],
    )
    tally = fairness.counts(result, [hoikushi, principal], slot_list, standard=standard)
    assert "S900" not in tally, (
        "配置対象外の職員が公平性集計に含まれている（最小値が構造的に 0 に固定される）"
    )
    assert "S001" in tally
    del slots, AgeClass, ChildPlan


import re  # noqa: E402  (下部テストで使用)
