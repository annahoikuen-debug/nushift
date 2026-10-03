"""``shiftai.ui.state`` の直接テスト。

``AppTest`` は使わず、``st.session_state`` を直接触って
``init_state`` / ``get`` / ``set`` / ``reset`` / ``reset_all`` /
``editing_guard`` / ``current_*`` 系を 1 関数ずつ検証する。

保証すること:

* ``init_state`` が ``DEFAULT_KEYS`` のキーを **全部** 埋め、初期値が妥当であること
* 一度初期化した後に ``init_state`` を再度呼んでも **既存値が残る** こと
* ``get`` が「既定値のコピー」を返すので、戻り値を壊しても state が汚らないこと
* ``normalize_fixed`` / ``weights_from_state`` などの変換関数が境界値でも壊れないこと
"""

from __future__ import annotations

import logging
from datetime import date, time
from typing import Any

import pytest
import streamlit as st

from shiftai import local_rules, solver
from shiftai.domain import (
    CellState,
    FacilitySettings,
    ObjectiveWeights,
    Slot,
    StaffingStandard,
    build_slots,
)
from shiftai.ui import state

#: ヘッドレス実行時の「missing ScriptRunContext!」警告でテスト出力を濁さないようにする。
logging.getLogger("streamlit").setLevel(logging.ERROR)

pytestmark = pytest.mark.timeout(300)


@pytest.fixture(autouse=True)
def clean_session_state():
    """各テストの開始時に session_state を空にして、テスト間の依存をなくす。"""
    st.session_state.clear()
    state.init_state()
    yield
    st.session_state.clear()


# --------------------------------------------------------------------------
# 初期化
# --------------------------------------------------------------------------


def test_init_stateがDEFAULT_KEYSのキーを全て埋める():
    """``init_state`` 後に ``DEFAULT_KEYS`` のキーが 1 つも欠けていないこと。"""
    missing = [key for key in state.DEFAULT_KEYS if key not in st.session_state]
    assert not missing, f"初期化されていないキー: {missing}"


def test_init_stateの既定値はDEFAULT_KEYSと一致する():
    """スカラー既定値は ``DEFAULT_KEYS`` の値と厳密に一致すること。"""
    for key, expected in state.DEFAULT_KEYS.items():
        if key in (
            state.KEY_SETTINGS,
            state.KEY_WEIGHTS,
            state.KEY_STANDARD,
            state.KEY_PRESETS,
            state.KEY_SOLVER_NAMES,
        ):
            continue  # 派生値は下で個別に検証する
        assert st.session_state[key] == expected, key


@pytest.mark.parametrize(
    ("key", "expected_type"),
    [
        (state.KEY_SETTINGS, FacilitySettings),
        (state.KEY_WEIGHTS, ObjectiveWeights),
        (state.KEY_STANDARD, StaffingStandard),
    ],
)
def test_派生される既定値の型が妥当である(key: str, expected_type: type):
    """``settings`` / ``weights`` / ``standard`` は正しい型で初期化されること。"""
    value = st.session_state[key]
    assert isinstance(value, expected_type)


def test_settingsの初期値は週日だけが休業日である():
    """既定の園設定は年間休業日が日曜のみ（52 日）で、他の既定値が config と一致すること。"""
    settings = st.session_state[state.KEY_SETTINGS]
    assert settings.closed_days
    assert all(d.weekday() == 6 for d in settings.closed_days)
    assert len(settings.closed_days) == 52
    assert settings.granularity_min > 0
    assert settings.day_open < settings.day_close
    assert settings.labor_cost_per_hour > 0


def test_standardの初期値はデフォルトプリセットと一致する():
    """``standard`` は ``DEFAULT_PRESET_KEY`` の基準そのものであること。"""
    assert st.session_state[state.KEY_STANDARD_KEY] == local_rules.DEFAULT_PRESET_KEY
    expected = local_rules.get_standard(local_rules.DEFAULT_PRESET_KEY)
    assert st.session_state[state.KEY_STANDARD].name == expected.name
    assert st.session_state[state.KEY_STANDARD].break_minutes == expected.break_minutes


def test_presetsとsolver_namesが実際の定義と一致する():
    """プリセット一覧・ソルバ名は実体と一致している状態で取得できていること。"""
    assert st.session_state[state.KEY_PRESETS] == local_rules.list_presets()
    assert st.session_state[state.KEY_SOLVER_NAMES] == solver.available_solvers()


def test_空のコレクションが既定で空リストになる():
    """``children`` / ``staff`` / ``days`` などは「空で正しい」既定値を持つこと。"""
    for key in (
        state.KEY_CHILDREN,
        state.KEY_STAFF,
        state.KEY_DAYS,
        state.KEY_VIOLATIONS,
        state.KEY_PRESETS,
    ):
        assert isinstance(st.session_state[key], list)
    assert st.session_state[state.KEY_STAFF] == []
    assert st.session_state[state.KEY_DAYS] == []
    assert st.session_state[state.KEY_FIXED_ASSIGNMENTS] == {}
    assert st.session_state[state.KEY_FRAMES] == {}
    assert st.session_state[state.KEY_STANDARD_OVERRIDES] == {}
    assert st.session_state[state.KEY_SLOTS] == ()


def test_init_stateを再実行しても既存値が消えない():
    """2 回目以降の ``init_state`` はユーザが設定した値を上書きしない（実害のある回帰の防止）。"""
    marker = FacilitySettings(facility_name="消してはいけない園", granularity_min=15)
    st.session_state[state.KEY_SETTINGS] = marker
    st.session_state[state.KEY_DAYS] = [date(2026, 1, 1)]
    st.session_state[state.KEY_STAFF] = ["S001"]
    st.session_state[state.KEY_STANDARD_KEY] = "福岡市"

    state.init_state()

    assert st.session_state[state.KEY_SETTINGS] is marker
    assert st.session_state[state.KEY_DAYS] == [date(2026, 1, 1)]
    assert st.session_state[state.KEY_STAFF] == ["S001"]
    assert st.session_state[state.KEY_STANDARD_KEY] == "福岡市"


def test_init_stateはNoneの派生値だけを作り直す():
    """``settings`` を ``None`` に戻して再初期化すると既定値を作り直されること。"""
    st.session_state[state.KEY_SETTINGS] = None
    st.session_state[state.KEY_WEIGHTS] = None
    st.session_state[state.KEY_STANDARD] = None

    state.init_state()

    assert isinstance(st.session_state[state.KEY_SETTINGS], FacilitySettings)
    assert isinstance(st.session_state[state.KEY_WEIGHTS], ObjectiveWeights)
    assert isinstance(st.session_state[state.KEY_STANDARD], StaffingStandard)


def test_init_stateは空のpresetsとsolver_namesを取り直す():
    """空リストは既定値へ戻る（API が使えない環境でも UI は動く）。"""
    st.session_state[state.KEY_PRESETS] = []
    st.session_state[state.KEY_SOLVER_NAMES] = []

    state.init_state()

    assert st.session_state[state.KEY_PRESETS] == local_rules.list_presets()
    assert st.session_state[state.KEY_SOLVER_NAMES] == solver.available_solvers()


# --------------------------------------------------------------------------
# get / set / reset / reset_all
# --------------------------------------------------------------------------


def test_getは未設定の既知キーに既定値のコピーを返す():
    """``get`` は「同一インスタンス」ではなく **コピー** を返すこと。"""
    st.session_state.clear()
    first = state.get(state.KEY_STAFF)
    assert first == []
    first.append("S999")

    second = state.get(state.KEY_STAFF)
    assert second == [], "戻り値を書き換えても session_state は変化しない"


def test_getは未知のキーに引数の既定値を返す():
    """``DEFAULT_KEYS`` に無いキーは ``default`` 引数がそのまま返ること。"""
    assert state.get("存在しないキー") is None
    assert state.get("存在しないキー", 42) == 42


def test_getはNone格納キーのとき引数の既定値を返す():
    """``DEFAULT_KEYS`` の値が ``None`` のキーは ``default`` にフォールバックすること。"""
    st.session_state[state.KEY_SETTINGS] = None
    assert state.get(state.KEY_SETTINGS, "フォールバック") == "フォールバック"


def test_getは設定を保持した値をそのまま返す():
    """``None`` 以外が session_state に入っていればコピーを作らず返すこと。"""
    days = [date(2026, 9, 28)]
    st.session_state[state.KEY_DAYS] = days
    assert state.get(state.KEY_DAYS) is days


def test_setで書き込んだ値をgetで取得できる():
    """``set`` → ``get`` の往復が-identity を保つこと。"""
    sentinel = object()
    state.set("テスト用の任意キー", sentinel)
    assert state.get("テスト用の任意キー") is sentinel
    assert st.session_state["テスト用の任意キー"] is sentinel


def test_setはウィジェット所有キーを先に破棄する():
    """``WIDGET_KEYS`` は代入前に ``pop`` されるため、古い widget 値が残らないこと。"""
    assert "granularity_min" in state.WIDGET_KEYS
    st.session_state["granularity_min"] = 15
    state.set("granularity_min", 60)
    assert st.session_state["granularity_min"] == 60


def test_resetは既知キーを既定値に戻す():
    """``reset`` が管理下の値を既定値へ巻き戻すこと（Editable な値は失われる）。"""
    st.session_state[state.KEY_STAFF] = ["S001"]
    st.session_state[state.KEY_TIME_LIMIT_SEC] = 10

    state.reset(state.KEY_STAFF)
    state.reset(state.KEY_TIME_LIMIT_SEC)

    assert st.session_state[state.KEY_STAFF] == []
    assert st.session_state[state.KEY_TIME_LIMIT_SEC] == 60


def test_resetは未知のキーを削除する():
    """``DEFAULT_KEYS`` に無いキーは「既定値に直す」のではなく削除されること。"""
    st.session_state["一時キー"] = 1
    state.reset("一時キー")
    assert "一時キー" not in st.session_state


def test_resetは未知のウィジェットキーを破棄する():
    """``DEFAULT_KEYS`` に無いキーは ``WIDGET_KEYS`` の有無に関わらず削除されること。

    現状 ``WIDGET_KEYS`` と ``DEFAULT_KEYS`` に共通要素が無いため、
    ``reset`` 内の ``WIDGET_KEYS`` 分岐は到達不能である（副作用のあるバグではない）。
    """
    st.session_state["facility_name"] = "テスト園"
    state.reset("facility_name")
    assert "facility_name" not in st.session_state


def test_reset_allは管理下のキーだけを初期化する():
    """``reset_all`` でユーザ値が消え、派生値だけ正しい型で再計算されること。"""
    st.session_state[state.KEY_STAFF] = ["S001"]
    st.session_state["外部から来たキー"] = "残ってよい"

    state.reset_all()

    assert st.session_state[state.KEY_STAFF] == []
    assert st.session_state["外部から来たキー"] == "残ってよい"
    assert isinstance(st.session_state[state.KEY_SETTINGS], FacilitySettings)
    assert isinstance(st.session_state[state.KEY_STANDARD], StaffingStandard)


# --------------------------------------------------------------------------
# editing_guard
# --------------------------------------------------------------------------


def test_editing_guardは正常系で値を残す():
    """例外が起きなければブロック内の変更がそのまま残ること。"""
    state.set(state.KEY_STAFF, ["S001"])
    with state.editing_guard(state.KEY_STAFF):
        state.set(state.KEY_STAFF, ["S002", "S003"])
    assert st.session_state[state.KEY_STAFF] == ["S002", "S003"]


def test_editing_guardは例外時に直前の値へ巻き戻す():
    """読み込み失敗時に職員データが壊れた状態で残らないこと。"""
    original = ["S001", "S002"]
    state.set(state.KEY_STAFF, original)

    with pytest.raises(RuntimeError, match="unksnow"):
        with state.editing_guard(state.KEY_STAFF):
            state.set(state.KEY_STAFF, [])
            raise RuntimeError("unksnow")

    assert st.session_state[state.KEY_STAFF] == ["S001", "S002"]
    assert st.session_state[state.KEY_STAFF] is not original


def test_editing_guardは未設定キーでも例外を飲み込まず再送出する():
    """ガードは例外を隠さず、呼び出し側が表示できるように再送出すること。"""
    with pytest.raises(ValueError):
        with state.editing_guard("未設定"):
            raise ValueError("boom")
    assert st.session_state["未設定"] is None


# --------------------------------------------------------------------------
# current_* アクセサ
# --------------------------------------------------------------------------


def test_current_standardはstandardが無ければプリセットから補完する():
    """``standard`` が falsy のときは ``standard_key`` から取り直されること。"""
    st.session_state[state.KEY_STANDARD] = None
    st.session_state[state.KEY_STANDARD_KEY] = "福岡市"
    standard = state.current_standard()
    assert standard.name == local_rules.get_standard("福岡市").name


def test_current_standardはstandardがあるときはそれを優先する():
    """上乗せ済みの ``standard`` が保持されている限りそちらを使うこと。"""
    override = local_rules.build_standard("福岡市", {"min_staff_per_room": 5})
    st.session_state[state.KEY_STANDARD] = override
    assert state.current_standard() is override


def test_current_settingsはsettingsが無ければ既定を返す():
    """``settings`` が falsy でもクラッシュせず既定の園設定が返ること。"""
    st.session_state[state.KEY_SETTINGS] = None
    settings = state.current_settings()
    assert isinstance(settings, FacilitySettings)
    assert settings.day_open < settings.day_close


def test_current_daysは空なら空リストを返す():
    """``days`` が未設定・空のどちらでも ``list`` が返ること（``None`` を混ぜない）。"""
    st.session_state[state.KEY_DAYS] = []
    assert state.current_days() == []
    st.session_state.clear()
    assert state.current_days() == []


def test_current_slotsは園設定から時間帯を生成してキャッシュする():
    """1 回目だけ ``build_slots`` が走り、2 回目はキャッシュを返すこと。"""
    st.session_state[state.KEY_SETTINGS] = FacilitySettings(
        day_open=time(9, 0), day_close=time(11, 0), granularity_min=30
    )
    st.session_state[state.KEY_SLOTS] = ()

    first = state.current_slots()

    assert isinstance(first, tuple)
    assert first
    assert all(isinstance(s, Slot) for s in first)
    assert st.session_state[state.KEY_SLOTS] == first
    assert state.current_slots() == first


def test_current_slotsは開所閉所が不正なら空タプルを返す():
    """``build_slots`` が ``ValueError`` を投げても UI は空の時間帯で動くこと。"""
    st.session_state[state.KEY_SETTINGS] = FacilitySettings(
        day_open=time(18, 0), day_close=time(9, 0), granularity_min=30
    )
    st.session_state[state.KEY_SLOTS] = ()

    slots = state.current_slots()

    assert slots == ()
    assert st.session_state[state.KEY_SLOTS] == ()


def test_refresh_daysは指定日数ぶんの日付を保存する():
    """開始日と日数から連続した日付を作り ``days`` に保存すること。"""
    days = state.refresh_days(date(2026, 9, 28), 5)
    assert len(days) == 5
    assert days[0] == date(2026, 9, 28)
    assert days[-1] == date(2026, 10, 2)
    assert st.session_state[state.KEY_DAYS] == days


@pytest.mark.parametrize("count", [0, -5])
def test_refresh_daysは0以下を1日分に補正する(count: int):
    """日数が 0 以下でも最低 1 日の計画になる（例外にしない）こと。"""
    days = state.refresh_days(date(2026, 9, 28), count)
    assert days == [date(2026, 9, 28)]


def test_data_readyは職員1名以上でTrue():
    """0 人なら ``False``、1 人以上なら ``True`` を返すこと。"""
    st.session_state[state.KEY_STAFF] = []
    assert state.data_ready() is False
    st.session_state[state.KEY_STAFF] = ["S001"]
    assert state.data_ready() is True


def test_invalidate_pipelineは最適化結果だけを破棄する():
    """入力データ（職員・園児）は保持し、派生結果だけを既定値へ戻すこと。"""
    st.session_state[state.KEY_STAFF] = ["S001"]
    for key in (
        state.KEY_REQUIREMENTS,
        state.KEY_SOLVE_RESULT,
        state.KEY_GAP_REPORT,
        state.KEY_VIOLATIONS,
        state.KEY_FIXED_ASSIGNMENTS,
    ):
        st.session_state[key] = "ダミー"

    state.invalidate_pipeline()

    assert st.session_state[state.KEY_STAFF] == ["S001"]
    assert st.session_state[state.KEY_REQUIREMENTS] is None
    assert st.session_state[state.KEY_SOLVE_RESULT] is None
    assert st.session_state[state.KEY_GAP_REPORT] is None
    assert st.session_state[state.KEY_VIOLATIONS] == []
    assert st.session_state[state.KEY_FIXED_ASSIGNMENTS] == {}


# --------------------------------------------------------------------------
# normalize_fixed
# --------------------------------------------------------------------------


def test_normalize_fixedはISO文字列の日付をdateに変換する():
    """UI 側の ISO 文字列をソルバ要求の ``datetime.date`` に正規化すること。"""
    normalized = state.normalize_fixed({("S001", "2026-09-28", "09:00-09:30"): "勤務"})
    assert normalized == {("S001", date(2026, 9, 28), "09:00-09:30"): CellState.WORK}


def test_normalize_fixedはdateとCellStateをそのまま通す():
    """既に正しい型なら変換せず同じ値を返すこと（往復で壊れない）。"""
    source = {("S001", date(2026, 9, 28), "09:00-09:30"): CellState.BREAK}
    assert state.normalize_fixed(source) == source


@pytest.mark.parametrize(
    "source",
    [
        None,
        {},
        {("S001", "not-a-date", "09:00-09:30"): "勤務"},
        {("S001",): "勤務"},
        {"S001": "勤務"},
    ],
)
def test_normalize_fixedは不正な入力を落として空を返す(source: Any):
    """壊れたキーが 1 つあっても例外にせず、使える分だけを返すこと。"""
    assert state.normalize_fixed(source) == {}


def test_normalize_fixedは職員IDと時間帯をstrに揃える():
    """ソルバ側の比較で型がずれないよう ID と時間帯ラベルを ``str`` に揃えること。"""
    normalized = state.normalize_fixed({(1, date(2026, 9, 28), 3): CellState.OFF})
    assert list(normalized) == [("1", date(2026, 9, 28), "3")]


# --------------------------------------------------------------------------
# 重み・供給量
# --------------------------------------------------------------------------


def test_weights_from_stateはスライダー値を対象関数に反映する():
    """``weight_*`` ウィジェット値が ``ObjectiveWeights`` に落ちること。"""
    st.session_state["weight_shortfall_penalty"] = 1234.0
    st.session_state["weight_overstaff_penalty"] = 3.0

    weights = state.weights_from_state()

    assert isinstance(weights, ObjectiveWeights)
    assert weights.shortfall_penalty == pytest.approx(1234.0)
    assert weights.overstaff_penalty == pytest.approx(3.0)


def test_weights_from_stateはスライダー無しでも既定を返す():
    """スライダー未描画（キーが無い）ときは保存済みの重みをそのまま返すこと。"""
    base = ObjectiveWeights(shortfall_penalty=42.0)
    st.session_state[state.KEY_WEIGHTS] = base
    assert state.weights_from_state() is base


def test_weights_from_stateはWEIGHT_WIDGETSの全項目を扱える():
    """将来重みが増えてもウィジェット定義とずれないよう、全項目が反映されること。

    件数そのものは固定しない（公平性の重みなどが追加されるため）。
    """
    assert len(state.WEIGHT_WIDGETS) >= 5
    for name, _label, _lo, _hi, _step in state.WEIGHT_WIDGETS:
        st.session_state[f"weight_{name}"] = 7.0
    weights = state.weights_from_state()
    for name, _label, _lo, _hi, _step in state.WEIGHT_WIDGETS:
        assert getattr(weights, name) == pytest.approx(7.0), name


def test_sync_weightsは重みをsession_stateへ保存する():
    """``sync_weights`` が保存まで行うこと（タブ3 から読む側と整合する）。"""
    st.session_state["weight_consecutive_day_penalty"] = 11.0
    weights = state.sync_weights()
    assert st.session_state[state.KEY_WEIGHTS] is weights
    assert weights.consecutive_day_penalty == pytest.approx(11.0)


def test_staff_name_mapは職員IDと氏名の辞書になる(small_staff):
    """職員スライスの ``(ID, 氏名)`` 対応を作ること。"""
    st.session_state[state.KEY_STAFF] = list(small_staff)
    names = state.staff_name_map()
    assert names["S001"] == "保育士1"
    assert len(names) == len(small_staff)


def test_contract_hoursは休園日を除外して数える(small_staff, small_requirements):
    """休園日は在勤可能日数に数えないこと（ソルバと同一の式）。"""
    st.session_state[state.KEY_STAFF] = list(small_staff)
    st.session_state[state.KEY_REQUIREMENTS] = small_requirements
    st.session_state[state.KEY_SETTINGS] = FacilitySettings(closed_days=frozenset())
    assert state.contract_hours() == pytest.approx(
        solver.supply_hours(small_staff, small_requirements, {}, state.current_settings())
    )


def test_contract_hoursは職員0人なら0():
    """職員が 0 人のとき 0.0 を返すこと（0 除算しない）。"""
    st.session_state[state.KEY_STAFF] = []
    assert state.contract_hours() == 0.0


def test_supply_demand_ratioは供給0ならinfを返す():
    """供給可能人時 0・必要人時 > 0 のとき ``inf`` を返すこと。

    0.0 を返すと UI の分岐が ``st.success`` に落ち、
    構造的に不可能なのに「基準を満たせる状態です」と表示していた。
    """
    st.session_state[state.KEY_STAFF] = []
    assert state.supply_demand_ratio(100.0) == float("inf")


def test_supply_demand_ratioは必要人時と供給人時の比になる(small_staff, small_requirements):
    """1.0 を超えると構造的に不足する、という警告の根拠になっていること。"""
    st.session_state[state.KEY_STAFF] = list(small_staff)
    st.session_state[state.KEY_REQUIREMENTS] = small_requirements
    st.session_state[state.KEY_SETTINGS] = FacilitySettings(closed_days=frozenset())

    supply = state.contract_hours()
    assert supply > 0.0
    assert state.supply_demand_ratio(supply * 2.0) == pytest.approx(2.0)


def test_build_slotsは開所閉所をまたぐと不正になる():
    """``current_slots`` が ``ValueError`` を拾える前提が保たれていること。"""
    with pytest.raises(ValueError):
        build_slots(time(18, 0), time(9, 0), 30, strict=True)


# --------------------------------------------------------------------------
# 供給人時 / 必要・供給比（2026-09-29 の修正の回帰）
# --------------------------------------------------------------------------


def test_supply_demand_ratioは供給0のときinf():
    """供給可能人時 0・必要人時 > 0 のとき比は ``inf``（0.0 ではない）こと。

    以前は 0.0 を返していたため、UI の分岐が ``st.success`` に落ち、
    構造的に不可能なのに「基準を満たせる状態です」と表示していた。
    """
    st.session_state.clear()
    state.init_state()
    try:
        assert state.supply_demand_ratio(10.0) == float("inf")
    finally:
        st.session_state.clear()


def test_supply_demand_ratioは両方0なら0():
    """必要人時も供給も 0 なら ``0.0``（除算不能の別ケース）であること。"""
    st.session_state.clear()
    state.init_state()
    try:
        assert state.supply_demand_ratio(0.0) == 0.0
    finally:
        st.session_state.clear()


def test_contract_hoursはソルバと同じ式を使う(small_staff, small_requirements):
    """UI の供給人時が ``solver.supply_hours`` と一致すること。

    以前は UI 側が「週契約時間 × 日数/5.0」を独自に計算しており、
    ソルバ側と約 1.2〜1.3 倍乖離していた。
    """
    st.session_state.clear()
    state.init_state()
    try:
        st.session_state[state.KEY_STAFF] = small_staff
        st.session_state[state.KEY_REQUIREMENTS] = small_requirements
        assert state.contract_hours() == pytest.approx(
            solver.supply_hours(small_staff, small_requirements, {}, state.current_settings())
        )
    finally:
        st.session_state.clear()


def test_current_slotsは不正な時刻で理由を保持する():
    """開所 > 閉所のとき空タプルを返し、理由が保持されること。"""
    st.session_state.clear()
    state.init_state()
    try:
        st.session_state[state.KEY_SETTINGS] = FacilitySettings(
            day_open=time(18, 0), day_close=time(9, 0)
        )
        assert state.current_slots() == ()
        assert state.current_slots_error(), "理由が保持されていない"
    finally:
        st.session_state.clear()


def test_current_slotsは正常時に理由が空():
    """正常時は理由が空文字列であること。"""
    st.session_state.clear()
    state.init_state()
    try:
        assert state.current_slots()
        assert state.current_slots_error() == ""
    finally:
        st.session_state.clear()


# --------------------------------------------------------------------------
# 入力指紋（UI/UX 改善 案3）
# --------------------------------------------------------------------------


def _with_settings(**changes: Any) -> None:
    """既定の園設定から一部だけ差し替えた settings を session に入れる。"""
    base = FacilitySettings()
    st.session_state[state.KEY_SETTINGS] = FacilitySettings(
        facility_name=changes.get("facility_name", base.facility_name),
        day_open=changes.get("day_open", base.day_open),
        day_close=changes.get("day_close", base.day_close),
        granularity_min=changes.get("granularity_min", base.granularity_min),
        closed_days=changes.get("closed_days", base.closed_days),
        holiday_dates=changes.get("holiday_dates", base.holiday_dates),
        labor_cost_per_hour=changes.get("labor_cost_per_hour", base.labor_cost_per_hour),
    )


def test_同じ設定なら同じ指紋になる():
    """同じ入力からは必ず同じ指紋が返ること。"""
    st.session_state.clear()
    state.init_state()
    try:
        assert state.compute_fingerprint() == state.compute_fingerprint()
    finally:
        st.session_state.clear()


def test_計画期間が変わると指紋が変わる():
    """計画期間を増やすと指紋が変わること。"""
    st.session_state.clear()
    state.init_state()
    try:
        before = state.compute_fingerprint()
        st.session_state[state.KEY_DAYS] = [date(2026, 10, 1), date(2026, 10, 2)]
        after = state.compute_fingerprint()
        assert before != after
    finally:
        st.session_state.clear()


def test_計画期間を元に戻すと指紋に戻る():
    """変更を戻したら指紋も元に戻ること（並びが安定していることの確認）。"""
    st.session_state.clear()
    state.init_state()
    try:
        before = state.compute_fingerprint()
        st.session_state[state.KEY_DAYS] = [date(2026, 10, 1)]
        st.session_state[state.KEY_DAYS] = []
        assert state.compute_fingerprint() == before
    finally:
        st.session_state.clear()


def test_開所時刻が変わると指紋が変わる():
    """開所時刻は時間帯全体をずらすので指紋が変わること。"""
    st.session_state.clear()
    state.init_state()
    try:
        before = state.compute_fingerprint()
        _with_settings(day_open=time(8, 15))
        assert state.compute_fingerprint() != before
    finally:
        st.session_state.clear()


def test_年間休業日が変わると指紋が変わる():
    """休業日は必要人員を消すので指紋が変わること。"""
    st.session_state.clear()
    state.init_state()
    try:
        before = state.compute_fingerprint()
        _with_settings(closed_days=frozenset({date(2026, 10, 1)}))
        assert state.compute_fingerprint() != before
    finally:
        st.session_state.clear()


def test_変更理由と現在指紋が記録される():
    """``mark_inputs_changed`` が理由を記録し、計算結果を破棄すること。"""
    st.session_state.clear()
    state.init_state()
    try:
        st.session_state[state.KEY_SOLVE_RESULT] = object()
        st.session_state[state.KEY_REQUIREMENTS] = object()
        state.mark_inputs_changed("計画期間を変更しました")
        assert state.get(state.KEY_STALE_REASON) == "計画期間を変更しました"
        assert state.get(state.KEY_SOLVE_RESULT) is None
        assert state.get(state.KEY_REQUIREMENTS) is None
        assert state.get(state.KEY_INPUT_FINGERPRINT) == state.compute_fingerprint()
    finally:
        st.session_state.clear()


def test_指紋は画面に出しても邪魔にならない長さで返る():
    """指紋は画面やログに出しても邪魔にならない長さであること。"""
    st.session_state.clear()
    state.init_state()
    try:
        value = state.compute_fingerprint()
        assert isinstance(value, str)
        assert value
        assert len(value) == 16
    finally:
        st.session_state.clear()
