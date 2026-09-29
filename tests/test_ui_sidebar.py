"""``shiftai.ui.sidebar`` の統合テスト（``streamlit.testing.v1.AppTest``）。

保証すること:

* サイドバー全体の描画が例外を投げないこと
* 園名・開所時刻・粒度などの設定変更が ``state`` に反映されること
* **プリセット選択の選択肢が ``local_rules.list_presets()`` と一致**すること
  （1 つ欠けたりずれたりすると選択中の基準が黙って変わるため）
* GAS 連携は **通信せずに** 設定の有無と失敗時の表示だけを検証する
  （``GoogleAppsScriptClient`` を差し替えてネットワークに出ないようにする）
"""

from __future__ import annotations

import logging
import pickle
from datetime import date, time
from pathlib import Path
from typing import Any

import pytest

from shiftai import local_rules
from shiftai.config import DEFAULT_RANGE_DAYS
from shiftai.domain import FacilitySettings
from shiftai.ui import state

#: ヘッドレス実行時の「missing ScriptRunContext!」警告でテスト出力を濁さないようにする。
logging.getLogger("streamlit").setLevel(logging.ERROR)

pytestmark = pytest.mark.timeout(300)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
APP_FILE = ROOT / "streamlit_app.py"

#: テスト中に GAS 連携を有効にするための環境変数（``conftest.GAS_ENV_NAMES`` と同じ）。
GAS_URL = "https://example.invalid/exec"

app_test = pytest.importorskip("streamlit.testing.v1", reason="AppTest が無い環境ではスキップ")


def _sidebar_script(tmp_path: Path, body: str, *, gas_on: bool = False) -> Path:
    """サイドバー描画スクリプトを tmp に生成する。"""
    env = (
        f"import os\nos.environ['SHIFTAI_GAS_URL'] = {GAS_URL!r}\n"
        "os.environ['SHIFTAI_GAS_SHEET'] = 'attendance'\n"
        if gas_on
        else "import os\nfor _k in ('SHIFTAI_GAS_URL', 'SHIFTAI_GAS_SHEET'):\n"
        "    os.environ.pop(_k, None)\n"
    )
    script = tmp_path / "sidebar_driver_app.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        f"{env}"
        "import streamlit as st\n"
        "from shiftai.ui import sidebar, state, theme\n"
        "theme.apply_page_config()\n"
        "state.init_state()\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return script


def _run_sidebar(tmp_path: Path, body: str, *, gas_on: bool = False) -> Any:
    """サイドバーを描画する ``AppTest`` を 1 フレーム流して返す。"""
    at = app_test.AppTest.from_file(
        str(_sidebar_script(tmp_path, body, gas_on=gas_on)), default_timeout=60
    )
    at.run()
    return at


@pytest.fixture(scope="module")
def fresh_app():
    """起動直後の実際のアプリ（読み取り専用の共有スナップショット）。

    AppTest の 1 フレームぶんの実行コストが ~0.8s あり、状態を書き換えない
    アサーションを同じスナップショットから行うことで実行時間を抑えている。
    テストは**読むだけ**なので実行順に依存しない。
    """
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    return at


@pytest.fixture(autouse=True)
def clean_gas_env(monkeypatch):
    """GAS 連携を「未設定」が出発点になるように毎テスト整える。"""
    for name in ("SHIFTAI_GAS_URL", "SHIFTAI_GAS_SHEET", "SHIFTAI_GAS_SECRET", "SHIFTAI_GAS_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    yield


# --------------------------------------------------------------------------
# 1. 描画そのものと既定値
# --------------------------------------------------------------------------


def test_サイドバーが描画される(fresh_app):
    """実際のアプリのサイドバーが例外を投げずに描画されること。"""
    assert not fresh_app.exception, [str(e.value) for e in fresh_app.exception]
    assert "設定" in "\n".join(m.value for m in fresh_app.sidebar.markdown)


def test_サイドバーの全セクションが描画される(tmp_path):
    """園設定・配置基準・計画期間・最適化オプションの 4 節が揃うこと。"""
    at = _run_sidebar(tmp_path, "sidebar.render()")
    assert not at.exception, [str(e.value) for e in at.exception]
    body = "\n".join(m.value for m in at.markdown)
    for heading in ("園設定", "配置基準", "計画期間"):
        assert heading in body, f"{heading} が描画されていない"
    assert any("最適化オプション" in e.label for e in at.expander)


def test_初期描画で園設定が既定値で入る(tmp_path):
    """初期化直後の ``settings`` が既定の園設定型で、日付範囲が埋まっていること。"""
    at = _run_sidebar(tmp_path, "sidebar.render()")
    settings = at.session_state[state.KEY_SETTINGS]
    assert isinstance(settings, FacilitySettings)
    assert settings.day_open < settings.day_close
    assert settings.granularity_min in (15, 30, 60)
    assert len(at.session_state[state.KEY_DAYS]) == DEFAULT_RANGE_DAYS


def test_計画期間がサイドバーから設定できる(tmp_path):
    """開始日と日数を変えると ``days`` がその日数だけ再計算されること。"""
    at = _run_sidebar(tmp_path, "sidebar.render()")
    start = at.date_input(key="range_start")
    assert start is not None
    at.number_input(key="range_days").set_value(3).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    days = at.session_state[state.KEY_DAYS]
    assert len(days) == 3
    assert days[0] == at.date_input(key="range_start").value
    assert any(f"{len(days)} 日" in c.value for c in at.caption)


# --------------------------------------------------------------------------
# 2. プリセット（Off by one の退行防止）
# --------------------------------------------------------------------------


def _preset_label(record: dict[str, str]) -> str:
    """サイドバーのプリセット選択肢ラベル（``名称（要約）``）を再現する。"""
    return f"{record['name']}（{record['summary']}）"


def test_プリセットの選択肢がlist_presetsと一致する(fresh_app):
    """サイドバーの選択肢が ``local_rules.list_presets()`` と 1 対 1・同順で一致すること。

    1 つでも欠けたり並びが変わると、選択中の基準が黙って別のものになるため
    ``options``（整形済みラベル）を直接突き合わせる。
    """
    options = list(fresh_app.selectbox(key="standard_preset").options)
    expected = [_preset_label(p) for p in local_rules.list_presets()]
    assert options == expected


def test_プリセットのラベルに名称と要約が含まれる(fresh_app):
    """選択肢のラベルが ``名称（要約）`` 形式で、人間が読めること。"""
    options = list(fresh_app.selectbox(key="standard_preset").options)
    for record in local_rules.list_presets():
        assert _preset_label(record) in options


def test_既定のプリセットが選択されている(fresh_app):
    """既定選択は ``DEFAULT_PRESET_KEY`` で、``standard_key`` と一致すること。"""
    assert fresh_app.session_state[state.KEY_STANDARD_KEY] == local_rules.DEFAULT_PRESET_KEY
    assert fresh_app.selectbox(key="standard_preset").value["key"] == local_rules.DEFAULT_PRESET_KEY
    assert fresh_app.session_state[state.KEY_STANDARD].name == local_rules.DEFAULT_PRESET_KEY


def test_プリセットを変更すると基準と標準が更新される():
    """プリセットを切り替えると ``standard_key`` / ``standard`` が新しい自治体のものになること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    target = "福岡市"
    records = {p["key"]: p for p in local_rules.list_presets()}
    if target not in records:
        pytest.skip("福岡市プリセットが無い")
    at.selectbox(key="standard_preset").set_value(records[target]).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.KEY_STANDARD_KEY] == target
    assert at.session_state[state.KEY_STANDARD].name == local_rules.get_standard(target).name


def test_プリセット変更で最適化結果が破棄される():
    """基準を変えたら再計算が必要なので ``solve_result`` が消えること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    at.session_state[state.KEY_SOLVE_RESULT] = "ダミー"
    at.session_state[state.KEY_REQUIREMENTS] = "ダミー"
    presets = local_rules.list_presets()
    if len(presets) < 2:
        pytest.skip("プリセットが 1 つしかない")
    at.selectbox(key="standard_preset").set_value(presets[1]).run()
    assert at.session_state[state.KEY_SOLVE_RESULT] is None
    assert at.session_state[state.KEY_REQUIREMENTS] is None


def test_上乗せ設定が基準に反映される():
    """「保育室の最低配置人数」を変えると ``standard`` の値も変わること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    before = at.session_state[state.KEY_STANDARD].min_staff_per_room
    at.number_input(key="min_staff_per_room").set_value(4).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.KEY_STANDARD].min_staff_per_room == 4
    assert at.session_state[state.KEY_STANDARD].min_staff_per_room != before or before == 4


# --------------------------------------------------------------------------
# 3. 園設定の変更が state に反映される
# --------------------------------------------------------------------------


def test_園名を変更するとsettingsに反映される():
    """園名テキスト入力が ``FacilitySettings.facility_name`` に反映されること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    at.text_input(key="facility_name").set_value("あおば保育園").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.KEY_SETTINGS].facility_name == "あおば保育園"


def test_開所時刻を変更するとsettingsに反映される():
    """開所時刻の変更が ``day_open`` に、時間帯のキャッシュ再生成が行われること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    at.time_input(key="day_open").set_value(time(8, 0)).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    settings = at.session_state[state.KEY_SETTINGS]
    assert settings.day_open == time(8, 0)
    slots = at.session_state[state.KEY_SLOTS]
    assert slots
    assert slots[0].start == time(8, 0)


def _minutes(start: time, end: time) -> int:
    """``datetime.time`` の差を分にする（``time`` 同士は引けないため）。"""
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


def test_粒度を変更するとsettingsと時間帯が反映される():
    """粒度を変えると ``granularity_min`` と時間帯の長さが変わること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    at.select_slider(key="granularity_min").set_value(60).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.KEY_SETTINGS].granularity_min == 60
    slots = at.session_state[state.KEY_SLOTS]
    assert slots
    assert _minutes(slots[0].start, slots[0].end) == 60


def test_人件費を変更するとsettingsに反映される():
    """人件費目安の入力を ``labor_cost_per_hour`` に反映すること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    at.number_input(key="labor_cost_per_hour").set_value(2000.0).run()
    assert at.session_state[state.KEY_SETTINGS].labor_cost_per_hour == pytest.approx(2000.0)


def test_休業日を変更するとsettingsのclosed_daysに反映される():
    """年間休業日の選択が ``closed_days`` に反映されること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    target = date(2026, 12, 31)
    at.multiselect(key="closed_days").set_value([target]).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert target in at.session_state[state.KEY_SETTINGS].closed_days
    assert all(d.weekday() == 6 for d in at.session_state[state.KEY_SETTINGS].closed_days) is False


def test_開所閉所が不正でも落ちずに時間帯が空になる():
    """閉所時刻が開所より前でもクラッシュせず時間帯が空のままであること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    at.time_input(key="day_open").set_value(time(18, 0)).run()
    at.time_input(key="day_close").set_value(time(9, 0)).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.KEY_SETTINGS].day_close == time(9, 0)
    assert at.session_state[state.KEY_SLOTS] == ()


def test_開所閉所が不正なら理由のエラーを出す():
    """不正な開所・閉所時刻では原因を説明するエラーが出ること。

    以前は ``state.current_slots()`` が ``ValueError`` を握り潰して空タプルを
    返すため、sidebar の ``except ValueError`` 節が到達不能で、
    「開所・閉所時刻の設定が不正です」が一度も表示されていなかった。
    """
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    at.time_input(key="day_open").set_value(time(18, 0)).run()
    at.time_input(key="day_close").set_value(time(9, 0)).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("開所・閉所時刻の設定が不正です" in e.value for e in at.error), (
        f"エラーが出ていない: {at.error}"
    )


# --------------------------------------------------------------------------
# 4. 最適化オプション（重み）
# --------------------------------------------------------------------------


def test_重みスライダーがstateに反映される():
    """スライダーを動かすと ``weights`` の対応フィールドが変わること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    before = at.session_state[state.KEY_WEIGHTS].shortfall_penalty
    at.slider(key="weight_shortfall_penalty").set_value(1234.0).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.KEY_WEIGHTS].shortfall_penalty == pytest.approx(1234.0)
    assert at.session_state[state.KEY_WEIGHTS].shortfall_penalty != before


def test_ソルバの時間上限がstateに反映される():
    """時間制限スライダーが ``time_limit_sec`` に反映されること。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()
    at.slider(key="ui_time_limit_sec").set_value(120).run()
    assert at.session_state[state.KEY_TIME_LIMIT_SEC] == 120


def test_利用可能なソルバ名が表示される(fresh_app):
    """取得済みのソルバ名がキャプションに出る（空なら「なし」と分かる）こと。"""
    at = fresh_app
    assert any("利用可能なソルバ" in c.value for c in at.caption)
    names = at.session_state[state.KEY_SOLVER_NAMES]
    if names:
        assert names[0] in "\n".join(c.value for c in at.caption)
    else:
        assert any("なし" in c.value for c in at.caption)


# --------------------------------------------------------------------------
# 5. GAS 連携（通信しない）
# --------------------------------------------------------------------------


def test_gas未設定なら案内だけを出す(tmp_path):
    """環境変数が無ければ「未設定」とだけ表示して通信しないこと。"""
    at = _run_sidebar(tmp_path, "sidebar.render()")
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("未設定" in c.value for c in at.caption)
    assert "gas_ping" not in {b.key for b in at.button}


def test_gas設定済みならexpanderと接続先を出す(tmp_path):
    """接続先が設定されていれば expander と接続先 URL を表示すること。"""
    at = _run_sidebar(tmp_path, "sidebar.render()", gas_on=True)
    assert not at.exception, [str(e.value) for e in at.exception]
    keys = {b.key for b in at.button}
    assert "gas_ping" in keys
    assert "gas_push_tables" in keys
    assert any(GAS_URL in c.value for c in at.caption)


def test_gas_callは設定が無ければエラー表示になる(tmp_path):
    """``_gas_call`` は接続先が無ければ画面を落とさずエラーを出すこと。"""
    at = _run_sidebar(tmp_path, "sidebar._gas_call(lambda c: None, '疎通確認')")
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("SHIFTAI_GAS_URL" in e.value for e in at.error)


def test_gas_callは失敗を画面側で抑えられる(tmp_path):
    """通信例外は ``GasError`` / 想定外の両方で ``st.error`` に変換されること。"""
    body = (
        "import shiftai.gas_client as gas\n"
        "class _Broken:\n"
        "    def __init__(self, config):\n"
        "        self.config = config\n"
        "    def ping(self):\n"
        "        raise gas.GasError('接続できません')\n"
        "gas.GoogleAppsScriptClient = _Broken\n"
        "sidebar._gas_call(lambda c: c.ping(), '疎通確認')\n"
    )
    at = _run_sidebar(tmp_path, body, gas_on=True)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("疎通確認 に失敗しました" in e.value for e in at.error)


def test_gas_callは想定外の例外もst_errorに変える(tmp_path):
    """``GasError`` 以外の例外で画面が落ちないこと（回帰防止）。"""
    body = (
        "import shiftai.gas_client as gas\n"
        "class _Boom:\n"
        "    def __init__(self, config):\n"
        "        pass\n"
        "    def ping(self):\n"
        "        raise RuntimeError('想定外')\n"
        "gas.GoogleAppsScriptClient = _Boom\n"
        "sidebar._gas_call(lambda c: c.ping(), '疎通確認')\n"
    )
    at = _run_sidebar(tmp_path, body, gas_on=True)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("予期しないエラー" in e.value for e in at.error)


def test_gas_pushはデータが未投入なら警告になる(tmp_path):
    """園児・職員が未投入のときは送信せずに警告すること。"""
    at = _run_sidebar(tmp_path, "sidebar._push_tables()", gas_on=True)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("先に園児・職員データ" in w.value for w in at.warning)


def test_gas_pushは設定が無ければエラーになる(tmp_path):
    """接続先が無い状態で送信しようとしても通信しないこと。"""
    at = _run_sidebar(tmp_path, "sidebar._push_tables()")
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("SHIFTAI_GAS_URL" in e.value for e in at.error)


def test_gas_pushは失敗をエラー表示に変える(tmp_path):
    """送信中の例外が ``st.error`` になり画面に投げ出されないこと。"""
    body = (
        "from datetime import date, time as dtime\n"
        "from shiftai.domain import AgeClass, ChildPlan, Role, StaffMember\n"
        "from tests.conftest import part_contract\n"
        "member = StaffMember(staff_id='S001', name='太郎', roles=(Role.HOIKUSHI,), contract=part_contract())\n"
        "child = ChildPlan('C001', '花子', date(2026, 9, 28), AgeClass.INFANT, dtime(9, 0), dtime(13, 0))\n"
        "st.session_state[state.KEY_STAFF] = [member]\n"
        "st.session_state[state.KEY_CHILDREN] = [child]\n"
        "st.session_state[state.KEY_DAYS] = [date(2026, 9, 28)]\n"
        "import shiftai.gas_client as gas\n"
        "class _Broken:\n"
        "    def __init__(self, config):\n"
        "        pass\n"
        "    def sync_all(self, tables):\n"
        "        raise gas.GasError('送信できません')\n"
        "gas.GoogleAppsScriptClient = _Broken\n"
        "sidebar._push_tables()\n"
    )
    at = _run_sidebar(tmp_path, body, gas_on=True)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("送信に失敗しました" in e.value for e in at.error)


# --------------------------------------------------------------------------
# 6. ウィジェット所有キーの不変条件
# --------------------------------------------------------------------------


def test_サイドバー描画後もWIDGET_KEYSがsession_stateに残る(tmp_path):
    """サイドバーはウィジェットを自分で作るので、対応するキーは生成済みであること。"""
    at = _run_sidebar(tmp_path, "sidebar.render()")
    keys = set(at.session_state.filtered_state)
    for key in ("facility_name", "day_open", "day_close", "granularity_min", "range_start", "range_days"):
        assert key in keys, f"{key} が session_state に無い"


def test_pickle可能なfacility_settingsである():
    """sidebar が session_state に置く ``FacilitySettings`` は pickle 可能であること。"""
    settings = FacilitySettings(day_open=time(9, 0), day_close=time(15, 0))
    restored = pickle.loads(pickle.dumps(settings))
    assert restored.day_open == settings.day_open
    assert restored.closed_days == settings.closed_days
