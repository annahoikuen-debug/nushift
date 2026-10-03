"""``shiftai.ui.tab_*`` の統合テスト（``streamlit.testing.v1.AppTest``）。

方針:

* **データ未投入**（``st.session_state`` が空）が最優先の検証対象。各タブが
  「案内（info/warning）」を出すだけで済むことを確認する。
* データ投入済みの描画は、**session_state を pickle で流し込むドライバスクリプト**
  で再現する。UI 内での最適化（MILP）は 1 回数十秒かかるため、実際に
  ``at.button(...).click()`` する一連の流れは ``@pytest.mark.slow`` にする。
* Streamlit サーバーは起動しない。ネットワークにも出ない。
"""

from __future__ import annotations

import dataclasses
import logging
import pickle
from datetime import date, time
from pathlib import Path
from typing import Any

import pytest
import streamlit as st

from shiftai import gap_analysis
from shiftai.domain import (
    ChildPlan,
    FacilitySettings,
    RequirementTable,
    SolveResult,
    SolveStatus,
    StaffPreferences,
    Violation,
    ViolationSeverity,
)
from shiftai.ui import state, tab_data, tab_export, wizard
from tests.conftest import DAY_CLOSE, DAY_OPEN, STANDARD_KEY  # noqa: E402  (計画値との整合)

#: ヘッドレス実行時の「missing ScriptRunContext!」警告でテスト出力を濁さないようにする。
logging.getLogger("streamlit").setLevel(logging.ERROR)

pytestmark = pytest.mark.timeout(600)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
APP_FILE = ROOT / "streamlit_app.py"

app_test = pytest.importorskip("streamlit.testing.v1", reason="AppTest が無い環境ではスキップ")

#: ドライバで描画できるタブ（モジュール名）。
TAB_MODULES: tuple[str, ...] = (
    "tab_data",
    "tab_requirements",
    "tab_solve",
    "tab_shift",
    "tab_export",
)

DRIVER_HEAD = """import pickle
import sys
sys.path.insert(0, {src!r})
sys.path.insert(0, {root!r})
import streamlit as st
from shiftai.ui import sidebar, state, theme
from shiftai.ui import {tab_imports}
theme.apply_page_config()
state.init_state()
PAYLOAD = pickle.load(open({blob!r}, "rb"))
for _key, _value in PAYLOAD.get("widgets", {{}}).items():
    st.session_state[_key] = _value
for _key, _value in PAYLOAD.get("state", {{}}).items():
    st.session_state[_key] = _value
import streamlit_app
streamlit_app.render_header()
"""


def _run_tabs(
    tmp_path: Path,
    tabs: tuple[str, ...],
    *,
    payload: dict[str, Any] | None = None,
    sidebar_on: bool = True,
) -> Any:
    """指定タブを描画するスクリプトを tmp に作り、1 フレーム流して ``AppTest`` を返す。"""
    assert all(name in TAB_MODULES for name in tabs)
    tmp_path.mkdir(parents=True, exist_ok=True)
    blob = tmp_path / "tab_payload.pkl"
    blob.write_bytes(pickle.dumps(payload or {}))
    body = DRIVER_HEAD.format(
        src=str(SRC),
        root=str(ROOT),
        tab_imports=", ".join(tabs),
        blob=str(blob),
    )
    if sidebar_on:
        body += "sidebar.render()\n"
    for name in tabs:
        body += f"{name}.render()\n"
    body += "theme.render_footer()\n"
    script = tmp_path / "tab_driver_app.py"
    script.write_text(body, encoding="utf-8")
    at = app_test.AppTest.from_file(str(script), default_timeout=120)
    at.run()
    return at


def _markdown_text(at: Any) -> str:
    """描画された markdown を 1 つの文字列にまとめる。"""
    return "\n".join(m.value for m in at.markdown)


def _all_text(at: Any) -> str:
    """描画されたテキストをすべて 1 つの文字列にまとめる。

    ``st.subheader`` は ``AppTest.subheader``、``st.expander`` のラベルは
    ``AppTest.expander`` に入るので、``_markdown_text`` だけでは
    埋め込み時の小見出しや基準充足マトリクスを見られない。
    """
    parts = [m.value for m in at.markdown]
    parts += [s.value for s in getattr(at, "subheader", [])]
    parts += [e.label for e in getattr(at, "expander", [])]
    parts += [t.value for t in getattr(at, "title", [])]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# フィクスチャ（conftest のものを再利用し、名前先頭地位合いを避ける）
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def plan_widgets():
    """サイドバーのウィジェット初期値。``small_requirements`` と整合させる。"""
    from tests.conftest import DAY_CLOSE, DAY_OPEN, STANDARD_KEY

    return {
        "facility_name": "テスト園",
        "day_open": DAY_OPEN,
        "day_close": DAY_CLOSE,
        "granularity_min": 30,
        "closed_days": [],
        "range_start": date(2026, 9, 28),
        "range_days": 1,
        "labor_cost_per_hour": 1500.0,
        "standard_key": STANDARD_KEY,
    }


@pytest.fixture(scope="module")
def input_payload(plan_widgets, small_children, small_staff, small_preferences, one_day):
    """「データ投入」まで済んだ状態の payload。"""
    from shiftai.data_loader import load_bundle
    from shiftai.sample_data import sample_dataframes

    frames = sample_dataframes(
        one_day, children=small_children, staff=small_staff, preferences=small_preferences
    )
    load_result = load_bundle(
        children_df=frames["children"],
        staff_df=frames["staff"],
        preferences_df=frames["preferences"],
    )
    return {
        "widgets": dict(plan_widgets),
        "state": {
            state.KEY_LOAD_RESULT: load_result,
            state.KEY_CHILDREN: list(small_children),
            state.KEY_STAFF: list(small_staff),
            state.KEY_PREFERENCES: dict(small_preferences),
            state.KEY_DAYS: list(one_day),
        },
    }


@pytest.fixture(scope="module")
def required_payload(input_payload, small_requirements):
    """「必要人員計算」まで済んだ状態の payload。"""
    payload = {key: dict(value) for key, value in input_payload.items()}
    payload["state"][state.KEY_REQUIREMENTS] = small_requirements
    return payload


@pytest.fixture(scope="module")
def solved_payload(
    required_payload, small_requirements, solved_day, small_staff, small_preferences, standard
):
    """「シフト作成」まで済んだ状態の payload。"""
    report = gap_analysis.analyze_gap(small_requirements, solved_day, small_staff)
    violations = gap_analysis.check_violations(
        small_requirements,
        solved_day,
        small_staff,
        small_preferences,
        standard=standard,
    )
    payload = {key: dict(value) for key, value in required_payload.items()}
    payload["state"][state.KEY_SOLVE_RESULT] = solved_day
    payload["state"][state.KEY_GAP_REPORT] = report
    payload["state"][state.KEY_VIOLATIONS] = violations
    return payload


@pytest.fixture(scope="module")
def empty_all_tabs(tmp_path_factory):
    """未投入のまま 5 タブすべてを描画した ``AppTest``（読み取り専用の共有スナップショット）。"""
    path = tmp_path_factory.mktemp("empty_app")
    return _run_tabs(path, TAB_MODULES, payload={}, sidebar_on=False)


@pytest.fixture(scope="module")
def input_app(tmp_path_factory, input_payload):
    """データ投入済みのタブ1 を描画した ``AppTest``（読み取り専用の共有スナップショット）。"""
    path = tmp_path_factory.mktemp("input_app")
    return _run_tabs(path, ("tab_data",), payload=input_payload)


@pytest.fixture(scope="module")
def requirements_app(tmp_path_factory, required_payload):
    """必要人員計算済みのタブ2 を描画した ``AppTest``（読み取り専用の共有スナップショット）。"""
    path = tmp_path_factory.mktemp("requirements_app")
    return _run_tabs(path, ("tab_requirements",), payload=required_payload)


@pytest.fixture(scope="module")
def solve_app(tmp_path_factory, solved_payload):
    """最適化済みのタブ3 を描画した ``AppTest``（読み取り専用の共有スナップショット）。"""
    path = tmp_path_factory.mktemp("solve_app")
    return _run_tabs(path, ("tab_solve",), payload=solved_payload)


@pytest.fixture(scope="module")
def shift_app(tmp_path_factory, solved_payload):
    """最適化済みのタブ4 を描画した ``AppTest``（読み取り専用の共有スナップショット）。"""
    path = tmp_path_factory.mktemp("shift_app")
    return _run_tabs(path, ("tab_shift",), payload=solved_payload)


@pytest.fixture(scope="module")
def export_app(tmp_path_factory, solved_payload):
    """最適化済みのタブ5 を描画した ``AppTest``（読み取り専用の共有スナップショット）。"""
    path = tmp_path_factory.mktemp("export_app")
    return _run_tabs(path, ("tab_export",), payload=solved_payload)


# --------------------------------------------------------------------------
# 1. データ未投入（最重要）
# --------------------------------------------------------------------------


def test_アプリ起動で全タブ例外なく描画される():
    """素の ``streamlit_app.py`` を 1 フレーム流して例外が無いこと。

    既定は初心者向けの **シンプル 3 タブ**（データ・シフト作成・出力）。
    """
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(at.tabs) == 3


def test_上級者モードで5タブ構成に戻る():
    """シンプルモードを OFF にすると従来の 5 タブ構成に戻ること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    assert len(at.tabs) == 3
    at.toggle(key="ui_simple_mode").set_value(False).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(at.tabs) == 5


def test_データ未投入では全タブが案内だけを出す():
    """``st.session_state`` が空でも info/warning で済み、error が出ないこと。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.error == [], "データ未投入でエラーを出してはいけない"
    assert at.info, "案内（info）を出すこと"
    assert at.session_state[state.KEY_STAFF] == []
    assert at.session_state[state.KEY_CHILDREN] == []


def test_データ未投入で5タブすべて例外なく描画される(empty_all_tabs):
    """未投入で 5 タブを同時に描いても例外が出ず、案内だけになること。"""
    assert not empty_all_tabs.exception, [str(e.value) for e in empty_all_tabs.exception]
    assert empty_all_tabs.error == []
    assert len(empty_all_tabs.info) + len(empty_all_tabs.warning) >= 5, (
        "タブごとに案内（info / warning）を出すこと"
    )


def test_データ未投入で必要人員タブは空状態案内を出す(empty_all_tabs):
    """タブ2 は「まずデータを投入してください」だけで終わること。"""
    assert any("データを投入" in info.value for info in empty_all_tabs.info)


def test_データ未投入でシフト作成タブは空状態案内を出す(empty_all_tabs):
    """タブ3 もクラッシュせず空状態案内に留まること。"""
    body = _markdown_text(empty_all_tabs) + "".join(i.value for i in empty_all_tabs.info)
    assert "データを投入" in body
    assert "シフトを自動作成する" in body


def test_未投入でシフト表と出力タブは作成ステップへの指示を出す(empty_all_tabs):
    """タブ4 とタブ5 が「先にシフトを作る」ことを案内すること。

    **契約 C1 の差し替え。** 以前は ``body.count("タブ3") >= 2`` を検査して
    いたが、シンプルモード（既定）にはタブが 3 本しかなく「タブ3」は出力タブの
    番号のことである。件数が合わ Forecasting 偶然通っていた。タブ番号そのもの
    を固定する代わりに、どちらのモードでも意味が通る文言を検査する。
    """
    assert not empty_all_tabs.exception, [str(e.value) for e in empty_all_tabs.exception]
    body = "".join(i.value for i in empty_all_tabs.info)
    assert body.count("まず") >= 2, "タブ4 とタブ5 の両方から誘導すること"
    assert body.count("シフト作成") >= 2, "シフト作成ステップへの指示が足りない"


def test_指示文は現在のモードのタブ番号を使う(tmp_path, input_payload):
    """シフト作成ステップを指すタブ番号が、モードに一致すること。

    **3 タブ構成にも「タブ3」は存在する（出力タブ）。** 従来の
    ``body.count("タブ3") >= 2`` が固定していたバグは、
    *シフト作成*をタブ3 と呼んでいたことだった。
    上級者モードではシフト作成がタブ3、シンプルモードではタブ2。
    """
    expert_payload = dict(input_payload)
    expert_payload["widgets"] = {**input_payload["widgets"], "ui_simple_mode": False}
    simple_dir = tmp_path / "simple"
    simple_dir.mkdir(parents=True, exist_ok=True)
    expert = _run_tabs(tmp_path, ("tab_shift",), payload=expert_payload)
    simple = _run_tabs(simple_dir, ("tab_shift",), payload=input_payload)
    assert not expert.exception, [str(e.value) for e in expert.exception]
    assert not simple.exception, [str(e.value) for e in simple.exception]
    expert_body = "".join(i.value for i in expert.info)
    simple_body = "".join(i.value for i in simple.info)
    assert "まずタブ3（シフト作成）" in expert_body
    assert "まずタブ2（シフト作成）" in simple_body
    assert "まずタブ3（シフト作成）" not in simple_body


def test_計画期間が空ならデータ投入タブが警告を出す(tmp_path):
    """``days`` が空のときは「サイドバーで計画期間を設定してください」と警告すること。"""
    at = _run_tabs(
        tmp_path,
        ("tab_data",),
        payload={"state": {state.KEY_DAYS: []}},
        sidebar_on=False,
    )
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("計画期間" in w.value for w in at.warning)


# --------------------------------------------------------------------------
# 2. データ投入タブ
# --------------------------------------------------------------------------


def test_データ投入タブは読み込み結果を表示する(input_app):
    """読み込み後にサマリ・期間カバレッジ・注意書きが表示されること。"""
    assert not input_app.exception, [str(e.value) for e in input_app.exception]
    assert "読み込み結果" in _markdown_text(input_app)
    assert any("園児データがカバーしている期間" in c.value for c in input_app.caption)


def test_データ投入タブは期間外の園児データを警告する(
    tmp_path, plan_widgets, small_children, small_staff, small_preferences
):
    """園児データが計画期間に及ばない日を明示すること。"""
    from shiftai.data_loader import load_bundle
    from shiftai.sample_data import sample_dataframes

    frames = sample_dataframes(
        [date(2026, 10, 5)],
        children=small_children,
        staff=small_staff,
        preferences=small_preferences,
    )
    load_result = load_bundle(
        children_df=frames["children"],
        staff_df=frames["staff"],
        preferences_df=frames["preferences"],
    )
    payload = {
        "widgets": {**plan_widgets, "range_days": 3},
        "state": {
            state.KEY_LOAD_RESULT: load_result,
            state.KEY_CHILDREN: list(small_children),
            state.KEY_STAFF: list(small_staff),
            state.KEY_PREFERENCES: dict(small_preferences),
            state.KEY_DAYS: [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)],
        },
    }
    at = _run_tabs(tmp_path, ("tab_data",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("園児データがありません" in w.value for w in at.warning)


def test_データ投入タブは職員0人を警告する(
    tmp_path, plan_widgets, small_children, small_staff, small_preferences
):
    """職員 0 名でも例外にせず「先に職員データ」と警告すること。"""
    from shiftai.data_loader import load_bundle
    from shiftai.sample_data import sample_dataframes

    frames = sample_dataframes(
        [date(2026, 9, 28)], children=small_children, staff=small_staff, preferences={}
    )
    load_result = load_bundle(
        children_df=frames["children"],
        staff_df=frames["staff"].head(0),
        preferences_df=frames["preferences"],
    )
    assert not load_result.staff
    payload = {
        "widgets": plan_widgets,
        "state": {
            state.KEY_LOAD_RESULT: load_result,
            state.KEY_CHILDREN: list(small_children),
            state.KEY_STAFF: [],
            state.KEY_PREFERENCES: {},
        },
    }
    at = _run_tabs(tmp_path, ("tab_data",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("職員データが 0 名" in w.value for w in at.warning)


def test_投入後のsession_stateがdomainの型になっている(input_app, small_children, small_staff):
    """UI 経由の投入結果が ``ChildPlan`` / ``StaffMember`` / ``StaffPreferences`` であること。"""
    assert not input_app.exception
    children = input_app.session_state[state.KEY_CHILDREN]
    members = input_app.session_state[state.KEY_STAFF]
    prefs = input_app.session_state[state.KEY_PREFERENCES]
    assert all(isinstance(child, ChildPlan) for child in children)
    assert all(isinstance(m, type(small_staff[0])) for m in members)
    assert all(isinstance(p, StaffPreferences) for p in prefs.values())
    assert len(members) == len(small_staff)
    assert len(children) == len(small_children)


# --------------------------------------------------------------------------
# 2-b. Undo/Redo と入力チェック（優先1）
# --------------------------------------------------------------------------


def test_データ投入タブはUndo_Historyボタンを出す(input_app):
    """各表に「元に戻す」「やり直す」が出ること。"""
    assert not input_app.exception, [str(e.value) for e in input_app.exception]
    keys = {b.key for b in input_app.button}
    for kind in ("children", "staff", "preferences"):
        assert f"undo_{kind}" in keys
        assert f"redo_{kind}" in keys


def test_データ投入タブは表の説明をツールチップに隠す(input_app):
    """説明と列一覧は常時表示のキャプションではなく、開いて初めて見えるチップにすること。

    初期画面の情報量を抑える要件（グレーの説明文を常時表示に出さない）の回帰防止。
    """
    assert not input_app.exception, [str(e.value) for e in input_app.exception]
    tips = [m.value for m in input_app.markdown if "shiftai-tip-body" in m.value]
    tips = [t for t in tips if "想定される列" in t]
    assert len(tips) == 3, "3 表ぶん（園児・職員・希望休）のツールチップがあること"
    assert any("1 プラン 1 行" in t for t in tips)
    # 常時表示側（キャプション）には説明を残さない。
    always_visible = [c.value for c in input_app.caption]
    assert not any("想定される列" in c or "1 プラン 1 行" in c for c in always_visible)


def test_データ投入タブは入力を検証して読み込みを止める(tmp_path, plan_widgets, one_day):
    """降園時刻が登園時刻より前の入力はエラーとして指摘し、読み込みボタンが無効になること。"""
    import pandas as pd

    bad = pd.DataFrame(
        [
            {
                "園児ID": "C001",
                "氏名": "園児1",
                "年齢": "3",
                "登園日": one_day[0],
                "登園時刻": time(15, 0),
                "降園時刻": time(9, 0),
                "短時間保育": "false",
                "欠席": "false",
                "欠席理由": "",
                "早朝保育": "false",
                "延長保育": "false",
                "備考": "",
            }
        ]
    )
    payload = {
        "widgets": dict(plan_widgets),
        "state": {
            state.KEY_DAYS: list(one_day),
            "frame_children": bad,
        },
    }
    at = _run_tabs(tmp_path, ("tab_data",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    errors = " ".join(e.value for e in at.error)
    assert "入力チェック" in errors
    assert (
        "降園時刻"
        in " ".join(
            str(v) for df in at.dataframe for v in (df.value if hasattr(df, "value") else "")
        )
        or "降園時刻" in errors
    )
    apply_button = next(b for b in at.button if b.key == "apply_load")
    assert apply_button.disabled is True
    report = at.session_state[state.KEY_VALIDATION]
    assert report is not None
    assert report.has_errors is True


def test_正常な入力なら読み込みボタンが押せる(
    tmp_path, plan_widgets, small_children, small_staff, small_preferences, one_day
):
    """指摘が無ければ読み込みボタンは有効であること。"""
    from shiftai.sample_data import sample_dataframes

    frames = sample_dataframes(
        one_day, children=small_children, staff=small_staff, preferences=small_preferences
    )
    payload = {
        "widgets": dict(plan_widgets),
        "state": {
            state.KEY_DAYS: list(one_day),
            "frame_children": frames["children"],
            "frame_staff": frames["staff"],
            "frame_preferences": frames["preferences"],
        },
    }
    at = _run_tabs(tmp_path, ("tab_data",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    apply_button = next(b for b in at.button if b.key == "apply_load")
    assert apply_button.disabled is False
    assert any("指摘はありません" in s.value for s in at.success)


def test_入力を戻すと履歴が積み上がる(tmp_path, plan_widgets, one_day):
    """表を書き換えると履歴が 1 件増えること。"""
    import pandas as pd

    frames = {
        "園児ID": ["C001", "C002"],
        "氏名": ["園児1", "園児2"],
        "年齢": ["3", "4"],
        "登園日": [one_day[0], one_day[0]],
        "登園時刻": [time(9, 0), time(9, 0)],
        "降園時刻": [time(15, 0), time(15, 0)],
        "短時間保育": ["false", "false"],
        "欠席": ["false", "false"],
        "欠席理由": ["", ""],
        "早朝保育": ["false", "false"],
        "延長保育": ["false", "false"],
        "備考": ["", ""],
    }
    payload = {
        "widgets": dict(plan_widgets),
        "state": {
            state.KEY_DAYS: list(one_day),
            "frame_children": pd.DataFrame(frames),
        },
    }
    at = _run_tabs(tmp_path, ("tab_data",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    history = at.session_state[state.KEY_EDIT_HISTORY]["children"]
    assert history.size >= 1


# --------------------------------------------------------------------------
# 3. 必要人員タブ
# --------------------------------------------------------------------------


def test_必要人員タブはKPIとヒートマップを描画する(requirements_app, small_requirements):
    """再計算済みの必要人員表で KPI・定員比・ヒートマップ・内訳表が出ること。"""
    assert not requirements_app.exception, [str(e.value) for e in requirements_app.exception]
    body = _markdown_text(requirements_app)
    assert "ピーク必要人員" in body
    assert "総必要人時" in body
    assert "時間帯 × 年齢クラスの必要人員" in body
    assert "必要人員の内訳" in body
    assert len(requirements_app.dataframe) >= 3
    assert requirements_app.session_state[state.KEY_REQUIREMENTS].slots == small_requirements.slots


def test_必要人員タブはnotesを展開する(requirements_app, small_requirements):
    """エンジンからの注記があれば expander で見せること。"""
    assert not requirements_app.exception, [str(e.value) for e in requirements_app.exception]
    if small_requirements.notes:
        assert any("エンジンからの注記" in e.label for e in requirements_app.expander)


def test_必要供給比が1を超えるとエラーを出す(
    tmp_path, plan_widgets, small_requirements, small_staff, one_day
):
    """供給可能人時が足りないと「どの組み合わせでも満たせない」エラーを出すこと。

    供給人時は「1日の契約時間 × 在勤可能日数」で計算される
    （``solver.supply_hours``。以前は UI だけが「週契約時間 × 日数/5」で
    計算していたため、ソルバと乖離していた）。したがって ``daily_hours``
    を下げないと不足を作れないので、両方を小さくする。
    """
    thin = [dataclasses.replace(small_staff[0])]
    thin[0] = dataclasses.replace(
        thin[0],
        contract=dataclasses.replace(thin[0].contract, weekly_hours=1.0, daily_hours=0.5),
    )
    payload = {
        "widgets": plan_widgets,
        "state": {
            state.KEY_STAFF: thin,
            state.KEY_REQUIREMENTS: small_requirements,
            state.KEY_DAYS: list(one_day),
        },
    }
    at = _run_tabs(tmp_path, ("tab_requirements",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("超えています" in e.value for e in at.error)


def test_開園日がないとき必要人員タブは案内だけを出す(tmp_path, plan_widgets, one_day, small_staff):
    """祝日で在園児 0 人のとき「在園児がいない」案内になりヒートマップを飛ばすこと。"""
    empty_table = RequirementTable(
        day_open=time(9, 0), day_close=time(11, 0), granularity_min=30, slots=(), rows={}
    )
    payload = {
        "widgets": plan_widgets,
        "state": {
            state.KEY_STAFF: list(small_staff),
            state.KEY_REQUIREMENTS: empty_table,
            state.KEY_DAYS: list(one_day),
        },
    }
    at = _run_tabs(tmp_path, ("tab_requirements",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("開園日がない" in i.value for i in at.info)


# --------------------------------------------------------------------------
# 3-c. 勤務パターンと診断パネル（優先2・優先3）
# --------------------------------------------------------------------------


def test_サイドバーは勤務パターンを設定できる(tmp_path, plan_widgets):
    """パターンを有効にすると、プリセットと枠ごとの時刻入力が出ること。"""
    payload = {
        "widgets": dict(plan_widgets),
        "state": {state.KEY_PATTERNS_ENABLED: True},
    }
    at = _run_tabs(tmp_path, ("tab_data",), payload=payload, sidebar_on=True)
    assert not at.exception, [str(e.value) for e in at.exception]
    labels = [s.label for s in at.selectbox]
    assert any("プリセット" in label for label in labels)
    keys = {w.key for w in at.time_input}
    assert {"pattern_start_0", "pattern_end_0"} <= keys
    patterns = at.session_state[state.KEY_PATTERNS]
    assert patterns, "パターンが保存されていない"
    assert all(p.end > p.start for p in patterns)


def test_パターンが無効なら一期分は空になる(tmp_path, plan_widgets):
    """無効のときはパターン定義を持たないこと（既存の挙動を変えない）。"""
    payload = {"widgets": dict(plan_widgets), "state": {}}
    at = _run_tabs(tmp_path, ("tab_data",), payload=payload, sidebar_on=True)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.KEY_PATTERNS] == ()
    assert at.session_state[state.KEY_PATTERNS_ENABLED] is False


def _expander_labels(at) -> list[str]:
    """``AppTest`` から展开セクションの見出しを取り出す。"""
    out: list[str] = []
    for exp in at.expander:
        for attr in ("label", "header", "title"):
            value = getattr(exp, attr, None)
            if isinstance(value, str):
                out.append(value)
                break
    return out


def test_シフト作成タブは不足理由の診断を出す(tmp_path, solved_payload):
    """「なぜ解けないのか」パネルと緩和モードの選択肢が出ること。"""
    at = _run_tabs(tmp_path, ("tab_solve",), payload=solved_payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("なぜ解けないのか" in label for label in _expander_labels(at))
    labels = [s.label for s in at.selectbox]
    assert any("どこまで緩めて" in label for label in labels)
    assert at.session_state[state.KEY_DIAGNOSIS] is not None


def test_シフト作成タブは勤務パターンの報告を出す(tmp_path, solved_payload):
    """パターンを有効にした結果には、整列の内訳が出ること。"""
    from shiftai.shift_patterns import DEFAULT_PATTERNS

    payload = dict(solved_payload)
    payload["state"] = {
        **solved_payload["state"],
        state.KEY_PATTERNS_ENABLED: True,
        state.KEY_PATTERNS: DEFAULT_PATTERNS,
        state.KEY_PATTERN_SNAP_REPORT: None,
    }
    at = _run_tabs(tmp_path, ("tab_solve",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("勤務パターンへの整列" in label for label in _expander_labels(at))


# --------------------------------------------------------------------------
# 4. シフト表・微調整タブ
# --------------------------------------------------------------------------


def test_シフト作成タブは未計算なら必要人員ステップへの指示を出す(tmp_path, input_payload):
    """必要人員未計算なら最適化ボタンを出さず、必要人員のステップへ誘導すること。

    **契約 C2 の差し替え。** 以前は「警告文に ``タブ2`` が含まれる」ことを
    検査していたが、シンプルモード（既定）には「タブ2 必要人員」が存在しない。
    固定文言がバグを固定していたので、モードごとに正しい指示を出すことを
    検査する。
    """
    at = _run_tabs(tmp_path, ("tab_solve",), payload=input_payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    warnings = " ".join(w.value for w in at.warning)
    assert "run_solve" not in {b.key for b in at.button}
    assert "必要人員" in warnings, "必要人員ステップへの指示がない"
    # シンプルモードには「タブ2 必要人員」が無いので出してはいけない。
    assert "タブ2" not in warnings, "3 タブ構成に無いタブ番号を案内している"


def test_上級者モードではタブ2への指示を出す(tmp_path, input_payload):
    """上級者モードでは「タブ2 必要人員」が実在するのでその番号で案内する。"""
    payload = dict(input_payload)
    payload["widgets"] = {**input_payload["widgets"], "ui_simple_mode": False}
    at = _run_tabs(tmp_path, ("tab_solve",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "run_solve" not in {b.key for b in at.button}
    assert "タブ2" in " ".join(w.value for w in at.warning)


def test_シフト作成タブは解があれば結果を一通り描画する(solve_app):
    """実行済みならバナー・KPI・過不足表・違反一覧・人員配置曲線が出ること。"""
    assert not solve_app.exception, [str(e.value) for e in solve_app.exception]
    body = _markdown_text(solve_app)
    assert "配置基準適合率" in body
    assert "配置不足時間帯数" in body
    assert "過不足テーブル" in body
    assert "人員配置曲線" in body
    assert "run_solve" in {b.key for b in solve_app.button}
    assert solve_app.session_state[state.KEY_SOLVE_RESULT].assignments
    assert solve_app.session_state[state.KEY_GAP_REPORT] is not None


def test_シフト作成タブは最適化後の状態をdomainの型で保持する(solve_app):
    """``solve_result`` / ``gap_report`` / ``violations`` が想定の型であること。"""
    result = solve_app.session_state[state.KEY_SOLVE_RESULT]
    report = solve_app.session_state[state.KEY_GAP_REPORT]
    violations = solve_app.session_state[state.KEY_VIOLATIONS]
    assert isinstance(result, SolveResult)
    assert isinstance(result.status, SolveStatus)
    assert hasattr(report, "coverage_ratio")
    assert all(hasattr(v, "severity") for v in violations)


# --------------------------------------------------------------------------
# Arrow 変換の回帰防止（描画は成功しても「表示が壊れている」ことがある）
#
# Streamlit の ``convert_pandas_df_to_arrow_table`` は、pyarrow が object 列を
# 推論できないと**例外を投げずに**警告だけ出し、「automatic fixes」で列を
# 変換して表示を続ける。テストからは「例外が出ない」ことしか確認できず、
# テストは全緑のまま画面が壊れる。
#
# 実際に起きている例: ``SolveResult.stats`` は型引数なしの dict で、
# 既定では ``relaxed_constraints`` / ``patterns`` が空リスト。
# それを「値」列にそのまま並べていたため、毎回 ArrowInvalid になっていた。
#
# そこで Streamlit が呼ぶ変換関数を包むフックを置き、**1 回目の変換が落ちたら
# 記録する**。描画自体は止めない（テストの他のアサーションが読めるように）。
# --------------------------------------------------------------------------


class _ArrowStrictRecorder:
    """``st.dataframe`` / ``st.data_editor`` に渡る DataFrame の Arrow 化失敗を記録する。

    Streamlit の変換関数を「素通し + 失敗記録」で差し替える。
    記録は :attr:`failures` に溜まる。
    """

    #: 記録の保持数（巨大な表を全部保持しない）。
    LIMIT = 10

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.checked = 0

    def __enter__(self) -> _ArrowStrictRecorder:
        import pyarrow as pa
        from streamlit import dataframe_util

        self._pa = pa
        self._original = dataframe_util.convert_pandas_df_to_arrow_bytes

        def wrapper(df, **kwargs):  # noqa: ANN001, ANN202
            self.checked += 1
            try:
                self._pa.Table.from_pandas(df)
            except Exception as exc:
                if len(self.failures) < self.LIMIT:
                    cols = ", ".join(f"{c}:{df[c].dtype}" for c in list(df.columns))
                    self.failures.append(f"{type(exc).__name__}: {exc} | columns=[{cols}]")
            return self._original(df, **kwargs)

        dataframe_util.convert_pandas_df_to_arrow_bytes = wrapper
        return self

    def __exit__(self, *exc_info: object) -> None:
        from streamlit import dataframe_util

        dataframe_util.convert_pandas_df_to_arrow_bytes = self._original


def test_シフト作成タブは表示する表をすべてArrow化できる(tmp_path, solved_payload):
    """タブ3 が描画する表は、そのまま Streamlit へ渡せること。

    「描画で例外が出ない」だけでは不十分（Streamlit は変換失敗を握り潰す）。
    """
    with _ArrowStrictRecorder() as recorder:
        at = _run_tabs(tmp_path, ("tab_solve",), payload=solved_payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert recorder.checked, "表が1件も描画されていない（検証できていない）"
    assert not recorder.failures, (
        "Streamlit が黙って表示を直した（automatic fixes）表がある:\n  "
        + "\n  ".join(recorder.failures)
    )


def test_シフト表タブは表示する表をすべてArrow化できる(tmp_path, solved_payload):
    """タブ4（シフト表・微調整）も同様。"""
    with _ArrowStrictRecorder() as recorder:
        at = _run_tabs(tmp_path, ("tab_shift",), payload=solved_payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert recorder.checked, "表が1件も描画されていない（検証できていない）"
    assert not recorder.failures, (
        "Streamlit が黙って表示を直した（automatic fixes）表がある:\n  "
        + "\n  ".join(recorder.failures)
    )


def test_確定済みセルがあると_tab3で通知する(tmp_path, solved_payload, small_staff, one_day):
    """手動確定したセルは再最適化でも動かない旨をタブ3 が伝えること。"""
    payload = {key: dict(value) for key, value in solved_payload.items()}
    payload["state"][state.KEY_FIXED_ASSIGNMENTS] = {
        (small_staff[0].staff_id, one_day[0], "09:00-09:30"): "勤務"
    }
    at = _run_tabs(tmp_path, ("tab_solve",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("手動で確定したセル" in i.value for i in at.info)


# --------------------------------------------------------------------------
# 5. シフト表タブ
# --------------------------------------------------------------------------


def test_シフト表タブはグリッドと微調整を描く(shift_app):
    """日別グリッド・職員別サマリ・編集表・曜日別ピボットが出ること。"""
    assert not shift_app.exception, [str(e.value) for e in shift_app.exception]
    body = _markdown_text(shift_app)
    assert "のシフト" in body
    assert "微調整" in body
    assert "曜日別サマリー" in body
    assert len(shift_app.dataframe) >= 4  # グリッド・サマリ・編集表・ピボット・基準充足
    assert any("職員別サマリ" in m.value for m in shift_app.markdown)
    assert {e.label for e in shift_app.expander} >= {"🔒 手動で確定したセル（0 件）"}


def test_シフト表タブはガント表示に切り替えられる(tmp_path, solved_payload):
    """表示形式を「ガントチャート」にすると横棒の HTML が出ること。"""
    at = _run_tabs(tmp_path, ("tab_shift",), payload=solved_payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    at = at.radio(key="shift_grid_view").set_value("ガントチャート").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    html = "".join(m.value for m in at.markdown)
    assert "shiftai-gantt-track" in html
    assert "shiftai-gantt-bar" in html


def test_シフト表タブは固定セル一覧を出す(tmp_path, solved_payload, small_staff, one_day):
    """確定セルがあると一覧が出て「すべて解除」ボタンが出ること。"""
    payload = {key: dict(value) for key, value in solved_payload.items()}
    payload["state"][state.KEY_FIXED_ASSIGNMENTS] = {
        (small_staff[0].staff_id, one_day[0], "09:00-09:30"): "勤務"
    }
    at = _run_tabs(tmp_path, ("tab_shift",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("手動で確定したセル" in e.label for e in at.expander)
    assert "clear_fixed" in {b.key for b in at.button}


def test_シフト表タブは職員0人なら警告する(tmp_path, solved_payload):
    """解があっても職員データが空なら警告して停止すること。"""
    payload = {key: dict(value) for key, value in solved_payload.items()}
    payload["state"][state.KEY_STAFF] = []
    at = _run_tabs(tmp_path, ("tab_shift",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("職員データが未投入" in w.value for w in at.warning)


def test_シフト表タブは時間帯を生成できないとエラーにする(tmp_path, solved_payload):
    """開所 > 閉所で時間帯が空なら明示的にエラー表示すること。"""
    payload = {key: dict(value) for key, value in solved_payload.items()}
    payload["widgets"]["day_open"] = time(18, 0)
    payload["widgets"]["day_close"] = time(9, 0)
    at = _run_tabs(tmp_path, ("tab_shift",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("時間帯を生成できません" in e.value for e in at.error)


# --------------------------------------------------------------------------
# 5-b. シンプルモードでのシフト表到達性（UI/UX 改善 案2）
# --------------------------------------------------------------------------


def test_シンプルモードでもシフト表セクションが描画される(
    tmp_path_factory, solved_payload
) -> None:
    """3 タブ構成の「② シフト作成」の中にシフト表が出ること。

    以前は ``tab_shift.render`` の呼び出しが ``else`` 枝の中だけにあり、
    既定のシンプルモードでは **シフト表が一度も描画されなかった**。
    タブ数 3 を保ったままセクションとして埋め込んだことを固定する。
    """
    path = tmp_path_factory.mktemp("simple_shift")
    blob = path / "solved_payload.pkl"
    blob.write_bytes(pickle.dumps(solved_payload))
    script = path / "simple_app.py"
    script.write_text(
        DRIVER_HEAD.format(
            src=str(SRC),
            root=str(ROOT),
            tab_imports="tab_solve",
            blob=str(blob),
        )
        + "tab_solve.render(auto_requirements=True, embed_shift=True)\n"
        + "theme.render_footer()\n",
        encoding="utf-8",
    )
    at = app_test.AppTest.from_file(str(script), default_timeout=180)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = _all_text(at)
    assert "シフト表・微調整" in body
    assert "曜日別サマリー" in body
    assert "微調整" in body


def test_3表だけのクリアは園設定を保つ(tmp_path, input_payload):
    """``reset_tables`` は 3 表だけ消し、園設定は残すこと。

    以前は「すべてクリア」が 1 つしかなく、
    ``state.reset_all()`` によって園名・開所閉所・基準・重みまで消えていた。
    """
    at = _run_tabs(tmp_path / "reset_tables", ("tab_data",), payload=input_payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    facility = at.session_state[state.KEY_SETTINGS].facility_name
    assert facility

    st.session_state.clear()
    state.init_state()
    try:
        settings = FacilitySettings(facility_name="消してはいけない園")
        st.session_state[state.KEY_SETTINGS] = settings
        st.session_state[state.KEY_STAFF] = ["dummy"]
        tab_data.reset_tables()
        assert st.session_state[state.KEY_SETTINGS].facility_name == "消してはいけない園"
    finally:
        st.session_state.clear()


def test_すべて初期化は園設定も消す(tmp_path):
    """``reset_everything`` はセッション全体を戻すこと（元の destructive な挙動）。"""
    st.session_state.clear()
    state.init_state()
    try:
        st.session_state[state.KEY_SETTINGS] = FacilitySettings(facility_name="消える園")
        tab_data.reset_everything()
        assert st.session_state[state.KEY_SETTINGS].facility_name != "消える園"
    finally:
        st.session_state.clear()


def test_実アプリのシンプルモードでシフト表が出る(
    solved_payload, one_day, small_children, small_staff, small_preferences,
    small_requirements, solved_day, standard,
) -> None:
    """``streamlit_app.py`` を 1 フレーム流して、シフト表がタブ②に出ること。

    ``_run_tabs`` はタブを直接呼ぶので、配線漏れを検出できない。
    既定（シンプルモード）で **アプリ全体** を通してもシフト表が
    出ることを確認する。UI/UX 改善 案2 の核心保証。
    """
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=240)
    for key, value in {
        "facility_name": "テスト園",
        "day_open": DAY_OPEN,
        "day_close": DAY_CLOSE,
        "granularity_min": 30,
        "closed_days": [],
        "range_start": one_day[0],
        "range_days": 1,
        "labor_cost_per_hour": 1500.0,
        "standard_key": STANDARD_KEY,
    }.items():
        at.session_state[key] = value
    at.session_state[state.KEY_LOAD_RESULT] = solved_payload["state"][
        state.KEY_LOAD_RESULT
    ]
    at.session_state[state.KEY_CHILDREN] = list(small_children)
    at.session_state[state.KEY_STAFF] = list(small_staff)
    at.session_state[state.KEY_PREFERENCES] = dict(small_preferences)
    at.session_state[state.KEY_DAYS] = list(one_day)
    at.session_state[state.KEY_REQUIREMENTS] = small_requirements
    at.session_state[state.KEY_SOLVE_RESULT] = solved_day
    at.run()

    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(at.tabs) == 3, "シンプルモードは 3 タブのまま"
    body = _all_text(at)
    for needle in ("シフト表・微調整", "微調整", "曜日別サマリー", "基準充足"):
        assert needle in body, f"タブ② に {needle} が無い"


def test_計画期間を変えると再計算を訴える(
    solved_payload, one_day, small_children, small_staff, small_preferences,
    small_requirements, solved_day,
) -> None:
    """日数を変えると上部の状態が「再計算が必要」に変わり、計算結果も消えること。

    以前はサイドバーの期間・時刻・定員比などを変えても
    ``invalidate_pipeline()`` が呼ばれず、古い必要人員表のまま
    最適化していた。UI/UX 改善 案3 の核心保証。
    """
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=240)
    for key, value in {
        "facility_name": "テスト園",
        "day_open": DAY_OPEN,
        "day_close": DAY_CLOSE,
        "granularity_min": 30,
        "closed_days": [],
        "range_start": one_day[0],
        "range_days": 1,
        "labor_cost_per_hour": 1500.0,
        "standard_key": STANDARD_KEY,
    }.items():
        at.session_state[key] = value
    at.session_state[state.KEY_LOAD_RESULT] = solved_payload["state"][
        state.KEY_LOAD_RESULT
    ]
    at.session_state[state.KEY_CHILDREN] = list(small_children)
    at.session_state[state.KEY_STAFF] = list(small_staff)
    at.session_state[state.KEY_PREFERENCES] = dict(small_preferences)
    at.session_state[state.KEY_DAYS] = list(one_day)
    at.session_state[state.KEY_REQUIREMENTS] = small_requirements
    at.session_state[state.KEY_SOLVE_RESULT] = solved_day
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # 実際に最適化を 1 回走らせる。基準の指紋は求解完了のタイミングで
    # 記録されるので，这条パスを通さないと初回の変更を検知できない。
    at.button(key="run_solve").click().run(timeout=600)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.KEY_SOLVE_RESULT] is not None
    assert at.session_state[state.KEY_INPUT_FINGERPRINT], "基準の指紋が記録されていない"

    # 日数を 1 -> 3 にする
    at.number_input(key="range_days").set_value(3).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    captions = [c.value for c in at.caption]
    assert any("再計算が必要です" in c for c in captions), (
        f"状態が更新されていない: {captions}"
    )
    assert at.session_state[state.KEY_STALE_REASON] == "計画期間を変更しました"
    assert at.session_state[state.KEY_SOLVE_RESULT] is None, "古い解が残っている"
    # シンプルモードの「② シフト作成」は ``auto_requirements=True`` なので、
    # 無効化された直後の同じフレームで新しい期間の必要人員表を作り直す。
    # したがって「None であること」ではなく「古い（1 日分の）表が残っていないこと」
    # を確認する。
    table = at.session_state[state.KEY_REQUIREMENTS]
    if table is not None:
        assert len(table.all_days()) == 3, (
            f"古い必要人員が残っている: {table.all_days()}"
        )


def test_シフト表セクションは例外なく描画される(tmp_path, solved_payload):
    """``render()`` 経由でもシフト表が従来どおり描画されること。"""
    at = _run_tabs(tmp_path / "in_simple", ("tab_shift",), payload=solved_payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    body = _markdown_text(at)
    assert "曜日別サマリー" in body
    assert len(at.dataframe) >= 4


def test_シフト表セクションは埋め込み時は小見出しだけを出す(
    tmp_path, solved_payload
) -> None:
    """``in_simple=True`` のときは番号つき大見出しを出さず、小見出しだけを出す。

    シンプルモードに「4」は存在しないため。``render()`` は上級者モード用の
    タブなので大見出しを出すが、``SIMPLE_INDEX`` によりシンプルモードでは
    「③ シフト作成」に解決される（HDL と 構成上一致する）。
    """
    path = tmp_path / "embedded"
    path.mkdir(parents=True, exist_ok=True)
    blob = path / "p.pkl"
    blob.write_bytes(pickle.dumps(solved_payload))
    script = path / "embedded_app.py"
    script.write_text(
        DRIVER_HEAD.format(
            src=str(SRC), root=str(ROOT), tab_imports="tab_shift", blob=str(blob)
        )
        + "tab_shift.render_section(in_simple=True)\n",
        encoding="utf-8",
    )
    at = app_test.AppTest.from_file(str(script), default_timeout=180)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = _all_text(at)
    assert "シフト表・微調整" in body
    assert "曜日別サマリー" in body
    assert "4. シフト表・微調整" not in body, "3 タブ構成に出ない番号を出している"


# --------------------------------------------------------------------------
# 6. 出力タブ
# --------------------------------------------------------------------------


def test_出力タブは各種ダウンロードを用意する(export_app):
    """CSV / Excel / ZIP / ICAL / Markdown のダウンロードボタンが揃い、実体も生成されること。"""
    assert not export_app.exception, [str(e.value) for e in export_app.exception]
    buttons = {b.key: b for b in export_app.get("download_button")}
    for expected in (
        "dl_shift_csv",
        "dl_payroll_csv",
        "dl_ics",
        "dl_requirements_csv",
        "dl_excel",
        "dl_zip",
        "dl_summary",
    ):
        assert expected in buttons, f"{expected} が無い"
        assert buttons[expected].proto.url, f"{expected} のファイルが生成されていない"
    assert "出力前の確認" in _markdown_text(export_app)


def _blocker_violation(day, slot) -> Violation:
    """出力前の確認を「未達」にする BLOCKER 違反を 1 件作る。"""
    return Violation(
        severity=ViolationSeverity.BLOCKER,
        code="SHORTFALL",
        message="配置基準を満たしていません",
        day=day,
        slot=slot,
        staff_id="S01",
    )


def test_確認未達ならダウンロードを無効にする(tmp_path, solved_payload, one_day, slots):
    """法令違反が残っているときはダウンロードを押せないようにすること。

    以前は「チェックは表示のみ」で、違反のあるシフトがそのまま
    ダウンロード・GAS 送信できていた。実体（``proto.url``）は
    **生成したまま** 無効化するので、既存の存在検査は壊れない。
    """
    payload = {key: dict(value) for key, value in solved_payload.items()}
    payload["state"][state.KEY_VIOLATIONS] = [_blocker_violation(one_day[0], slots[0])]
    at = _run_tabs(tmp_path / "gated", ("tab_export",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    buttons = {b.key: b for b in at.get("download_button")}
    assert buttons, "ダウンロードボタンが無い"
    for key, button in buttons.items():
        assert button.proto.disabled, f"{key} が無効化されていない"
        assert button.proto.url, f"{key} のファイルが生成されていない"
    assert any("出力前の確認に未達" in e.value for e in at.error)


def test_ダウンロードの無効化は確認リストに一致する(export_app):
    """ボタンの無効状態が「出力前の確認」の判定と一致すること。"""
    body = _markdown_text(export_app)
    unmet = "⚠️" in body
    assert "出力前の確認" in body
    for key, button in {b.key: b for b in export_app.get("download_button")}.items():
        assert button.proto.disabled is unmet, (
            f"{key} の無効状態が確認リストと一致しない（未達={unmet}）"
        )


def test_確認未達でも修正ボタンを出す(tmp_path, solved_payload, one_day, slots):
    """出力タブからも違反の修正対象にできるようにすること。"""
    payload = {key: dict(value) for key, value in solved_payload.items()}
    payload["state"][state.KEY_VIOLATIONS] = [_blocker_violation(one_day[0], slots[0])]
    at = _run_tabs(tmp_path / "fix_actions", ("tab_export",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    fix_buttons = [b for b in at.button if b.key.startswith("export_fix_")]
    assert fix_buttons, "出力タブに修正ボタンが無い"


def test_修正ボタンで対象を指定できる(tmp_path, solved_payload, one_day, slots):
    """修正ボタンを押すとシフト表側が受け取れる対象が入ること。"""
    payload = {key: dict(value) for key, value in solved_payload.items()}
    payload["state"][state.KEY_VIOLATIONS] = [_blocker_violation(one_day[0], slots[0])]
    at = _run_tabs(tmp_path / "fix_click", ("tab_export",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    target = at.button(key="export_fix_0")
    assert not target.proto.disabled
    target.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    fixed = at.session_state[state.KEY_FIX_TARGET]
    assert fixed is not None, "修正対象が session に入っていない"
    iso_day, staff_id, slot_label = fixed
    assert iso_day == one_day[0].isoformat()
    assert staff_id == "S01"
    assert slot_label


def test_修正対象は1回だけ消費される(tmp_path, solved_payload, one_day, slots):
    """同じ修正が毎回繰り返されないこと（``take_fix_target`` の役割）。"""
    st.session_state.clear()
    state.init_state()
    try:
        state.set_fix_target(("2026-09-28", "S01", "09:00-09:30"))
        assert state.take_fix_target() == ("2026-09-28", "S01", "09:00-09:30")
        assert state.take_fix_target() is None
    finally:
        st.session_state.clear()


def test_出力タブのプレビュー表が出揃う(export_app):
    """シフト・必要人員・給与・マトリクスのプレビューが揃うこと。"""
    labels = [e.label for e in export_app.expander]
    for name in ("シフト", "必要人員", "給与計算", "シフトマトリクス"):
        assert any(name in label for label in labels), f"{name} のプレビューが無い"


def test_出力タブは職員0人ならエラーにする(tmp_path, solved_payload):
    """解があっても職員が 0 人ならエラー表示で停止すること。"""
    payload = {key: dict(value) for key, value in solved_payload.items()}
    payload["state"][state.KEY_STAFF] = []
    at = _run_tabs(tmp_path, ("tab_export",), payload=payload)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("職員データまたは時間帯が未設定" in e.value for e in at.error)


def test_safe_nameはファイル名に使える文字だけを残す():
    """園名に空白・記号が混ざっても安全なファイル名になること。"""
    assert tab_export._safe_name("Aoba 2-1_2026.csv") == "Aoba_2-1_2026.csv"
    assert tab_export._safe_name("///") == "shiftai"
    assert tab_export._safe_name("   ") == "shiftai"


def test_safe_nameは日本語の園名を残す():
    """園名が日本語でもファイル名として残ることを確認する。

    以前は ASCII のみ許可していたため、既定の園名「あさひ保育園」が
    すべて削られ、全ダウンロードのファイル名が ``シフト_shiftai_...`` になっていた。
    """
    from shiftai.domain import FacilitySettings

    default_name = FacilitySettings().facility_name
    assert tab_export._safe_name(default_name) == "あさひ保育園"
    assert tab_export._safe_name("あおば 保育園/2") == "あおば_保育園_2"


def test_safe_nameはパス区切りと制御文字を落とす():
    """パス区切り・引用符・制御文字はファイル名に残さないこと。"""
    assert tab_export._safe_name('a\\b:c*d?e"f<g>h|i') == "a_b_c_d_e_f_g_h_i"
    assert tab_export._safe_name("a\x00b\x1fc") == "a_b_c"


def test_file_stemは既定の園名が使われる():
    """ファイル名の stem に園名が実際に含まれること（回帰: 園名が消えない）。"""
    from datetime import date as _date

    from shiftai.domain import FacilitySettings

    facility = FacilitySettings().facility_name
    st.session_state.clear()
    state.init_state()
    st.session_state[state.KEY_SETTINGS] = state.get(state.KEY_SETTINGS)
    st.session_state[state.KEY_DAYS] = [_date(2026, 9, 28), _date(2026, 9, 29)]
    try:
        stem = tab_export._file_stem("shift")
        assert stem.startswith(f"シフト_{facility}_")
        assert stem.endswith("_shift")
    finally:
        st.session_state.clear()


def test_file_stemは園名と期間を含む(tmp_path, solved_payload):
    """ファイル名の stem が ``シフト_<園名>_<開始>_<終了>_<種類>`` になること。"""
    from datetime import date as _date

    st.session_state.clear()
    state.init_state()
    st.session_state[state.KEY_SETTINGS] = state.get(state.KEY_SETTINGS)
    st.session_state[state.KEY_DAYS] = [_date(2026, 9, 28), _date(2026, 9, 29)]
    try:
        stem = tab_export._file_stem("shift")
        assert stem.startswith("シフト_")
        assert stem.endswith("_shift")
        assert "2026-09-28_2026-09-29" in stem
    finally:
        st.session_state.clear()


def test_file_stemは日が無くても今日で埋める():
    """計画期間が空でもファイル名が壊れないこと。"""
    st.session_state.clear()
    state.init_state()
    try:
        stem = tab_export._file_stem("ics")
        assert stem.endswith("_ics")
        assert date.today().isoformat() in stem
    finally:
        st.session_state.clear()


# --------------------------------------------------------------------------
# 7. 最適化後にタブ2を開いてもクラッシュしないこと（回帰）
# --------------------------------------------------------------------------


def test_最適化後にタブ2を開いてもクラッシュしない(tmp_path, solved_payload):
    """シフト作成後にタブ2 を開いても例外が出ないこと。

    以前は ``components.heat_styler`` が文字列列にも ``"{:.0f}"`` を適用して
    ``ValueError: Unknown format code 'f' for object of type 'str'`` で落ちていた。
    ``_render_gap_preview`` が日付列つきの ``gap_matrix`` を渡していたため、
    タブ3 の実行後の次の rerun で必ず再現していた。heat_styler は
    数値列にだけ fmt を適用するように修正済み。
    """
    at = _run_tabs(tmp_path, ("tab_requirements",), payload=solved_payload)
    errors = [str(e.value) for e in at.exception]
    assert not errors, errors
    assert "Unknown format code" not in "".join(e.value for e in at.error), [
        e.value for e in at.error
    ]


# --------------------------------------------------------------------------
# 8. 一連の流れ（slow: 実際に最適化が走る）
# --------------------------------------------------------------------------


@pytest.mark.slow
def test_サンプル投入からシフト作成まで一連が通る():
    """タブ1 のサンプル読込 → 読み込み → タブ2 再計算 → タブ3 最適化を実際にクリックして通すこと。

    UI 内で MILP が 1 回走るため ``slow`` 扱い（``-m "not slow"`` では除外される）。
    初期画面の「入力方法」を「まとめて入力」（従来の 3 表画面）に選んでから通す。
    ウィザード側の読み込みは ``test_ui_wizard`` が（高速に）検証する。
    """
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=180)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    # 1 日分に絞って計算量を抑える（プランの連続性は ``range_days`` ウィジェットが保証している）
    at.number_input(key="range_days").set_value(1).run()
    assert not at.exception, [str(e.value) for e in at.exception]

    at.radio(key=wizard.KEY_MODE).set_value(wizard.MODE_BULK).run()
    at.button(key="wizard_start").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]

    for key in ("sample_children", "sample_staff", "sample_preferences"):
        at.button(key=key).click().run()
        assert not at.exception, f"{key} の読込で失敗"

    at.button(key="apply_load").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.KEY_STAFF]
    assert at.session_state[state.KEY_CHILDREN]

    # シンプルモード（既定）にはタブ2「必要人員」が無い。
    # 最適化は内部で必要人員を自動再計算するため、そのまま実行できる。
    at.button(key="run_solve").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert isinstance(at.session_state[state.KEY_REQUIREMENTS], RequirementTable)
    assert not at.exception, [str(e.value) for e in at.exception]
    result = at.session_state[state.KEY_SOLVE_RESULT]
    assert isinstance(result, SolveResult)
    assert result.assignments, "1 日分の割当が 1 つも無いのはおかしい"
    assert at.session_state[state.KEY_GAP_REPORT] is not None
