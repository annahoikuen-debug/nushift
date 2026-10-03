"""``shiftai.ui.wizard``（初心者向け入力ウィザード）の回帰テスト。

優先したいのは「はじめて使う人が迷わない」ことなので、
実際の描画（``AppTest``）を通して

* 最初に入力方法（ウィザード／まとめて入力）を尋ねる
* ウィザードが 5 ステップで最後まで進める
* 計画期間がステップをまたいでも消えない
* サイドバーと同じキーのウィジェットを二重に作らない

を検証する。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import streamlit as st

from shiftai.ui import state, wizard

app_test = pytest.importorskip("streamlit.testing.v1", reason="AppTest が無い環境ではスキップ")

#: ヘッドレス実行時の「missing ScriptRunContext!」警告で出力を濁さないようにする。
logging.getLogger("streamlit").setLevel(logging.CRITICAL)

pytestmark = pytest.mark.timeout(600)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
APP_FILE = ROOT / "streamlit_app.py"


# --------------------------------------------------------------------------
# キー文字列の固定（UI/UX 改善 案3・ステップ12）
# --------------------------------------------------------------------------


def test_キー文字列はtab_data側と一致する() -> None:
    """``tab_data.reset_everything`` は ``"wizard_step"`` をリテラルで pop する。

    ``wizard`` が ``tab_data`` を import しているので ``tab_data`` 側から
    ``wizard`` を import すると循環する。そのためリテラルで書いており、
    ここがずれるとウィザードのステップだけ初期化されなくなる。
    """
    assert wizard.KEY_STEP == "wizard_step"


def test_すべて初期化でウィザードのステップが0に戻る() -> None:
    """``tab_data.reset_everything()`` で ``KEY_STEP`` が 0 へ戻ること。"""
    from shiftai.ui import tab_data

    st.session_state.clear()
    try:
        wizard.set_step(4)
        assert wizard.current_step() == 4
        tab_data.reset_everything()
        assert wizard.current_step() == 0
    finally:
        st.session_state.clear()


def test_3表だけのクリアはウィザードのステップを保つ(tmp_path) -> None:
    """``reset_tables`` はウィザードのステップを消さないこと。"""
    from shiftai.ui import tab_data

    st.session_state.clear()
    try:
        wizard.set_step(3)
        tab_data.reset_tables()
        assert wizard.current_step() == 3
    finally:
        st.session_state.clear()

WIZARD_DRIVER = """import sys
sys.path.insert(0, {src!r})
import streamlit as st
from shiftai.ui import state, theme, wizard
theme.apply_page_config()
state.init_state()
st.session_state[wizard.KEY_ANSWERED] = True
st.session_state[wizard.KEY_MODE] = {mode!r}
# 開始位置は「最初の 1 フレームだけ」決める。rerun のたびに上書きすると
# ウィザードの set_step() が無効化されてステップが進まなくなる。
if wizard.KEY_STEP not in st.session_state:
    st.session_state[wizard.KEY_STEP] = {step}
wizard.render()
"""


def _run_wizard(tmp_path: Path, *, step: int = 0, name: str = "wizard") -> object:
    """ウィザード画面を 1 フレーム描画した ``AppTest`` を返す。"""
    script = tmp_path / f"{name}_driver.py"
    script.write_text(
        WIZARD_DRIVER.format(src=str(SRC), step=step, mode=wizard.MODE_WIZARD),
        encoding="utf-8",
    )
    at = app_test.AppTest.from_file(str(script), default_timeout=120)
    at.run()
    return at


def _run_app() -> object:
    """``streamlit_app.py`` を 1 フレーム描画した ``AppTest`` を返す。"""
    at = app_test.AppTest.from_file(str(APP_FILE), default_timeout=180)
    at.run()
    return at


def _markdown(at: object) -> str:
    return "\n".join(m.value for m in at.markdown)


def _button_keys(at: object) -> set[str]:
    return {b.key for b in at.button if b.key}


# --------------------------------------------------------------------------
# 1. 最初に尋ねる
# --------------------------------------------------------------------------


def test_初回はウィザードで入力するか尋ねる():
    """最初の一画面は「ウィザードで順に入力する／まとめて入力する」の選択。"""
    at = _run_app()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "入力方法を選んでください" in _markdown(at)
    radios = [r for r in at.radio if r.label == "入力方法"]
    assert len(radios) == 1, "入力方法の選択肢は 1 つだけ（メイン画面側）"
    assert list(radios[0].options) == [
        "🧙 ウィザードで順に入力する（おすすめ）",
        "📋 まとめて入力する（貼り付け・上級者向け）",
    ]
    assert radios[0].value == wizard.MODE_WIZARD, "未回答の既定はウィザード"
    assert "wizard_start" in _button_keys(at)


def test_質問に答えるとウィザード画面が開く():
    """ "▶ この方法で入力する" でステップ 1 に入る。"""
    at = _run_app()
    at.button(key="wizard_start").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "ステップ 1: 園の条件" in _markdown(at)
    assert {"wizard_prev", "wizard_next"} <= _button_keys(at)
    assert "入力方法を選んでください" not in _markdown(at), "質問は一度だけ"


def test_まとめて入力を選ぶと従来の3表画面になる():
    """「まとめて入力」を選ぶとウィザードではなく 3 表の expander が出ること。"""
    at = _run_app()
    at.radio(key=wizard.KEY_MODE).set_value(wizard.MODE_BULK).run()
    at.button(key="wizard_start").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    keys = _button_keys(at)
    assert {"sample_children", "sample_staff", "sample_preferences"} <= keys
    assert "wizard_next" not in keys


# --------------------------------------------------------------------------
# 2. ウィザードのステップ
# --------------------------------------------------------------------------


def test_ステップは5つでチップに並ぶ():
    assert len(wizard.STEP_LABELS) == 5
    at = _run_wizard(_tmp(), step=0)
    chips = "\n".join(m.value for m in at.markdown if "shiftai-chip" in m.value)
    for label in wizard.STEP_LABELS:
        assert label in chips, f"{label} のチップが無い"


def test_次へでステップが進み戻るで戻る(tmp_path):
    at = _run_wizard(tmp_path, step=0)
    assert at.button(key="wizard_prev").disabled, "最初のステップでは戻れない"
    at.button(key="wizard_next").click().run()
    assert "ステップ 2: 園児" in _markdown(at)
    assert not at.button(key="wizard_prev").disabled
    at.button(key="wizard_prev").click().run()
    assert "ステップ 1: 園の条件" in _markdown(at)


def test_園児が空なら次へ進めない(tmp_path):
    """必要な表が空のときは進めず、理由を示すこと。"""
    at = _run_wizard(tmp_path, step=1)
    assert "ステップ 2: 園児" in _markdown(at)
    assert at.button(key="wizard_next").disabled
    assert any("が空です" in w.value for w in at.warning)


def test_職員が空なら次へ進めない(tmp_path):
    """職員 0 名ではシフトを組み立てられないため進めなくする。"""
    at = _run_wizard(tmp_path, step=2)
    assert at.button(key="wizard_next").disabled
    assert any("職員" in w.value and "が空です" in w.value for w in at.warning)


def test_希望休は空でも進める(tmp_path):
    """希望休は任意なので、空のまま次のステップへ進めること。"""
    at = _run_wizard(tmp_path, step=3)
    assert not at.button(key="wizard_next").disabled
    assert not [w for w in at.warning if "希望休" in w.value]


def test_園の条件はステップをまたいでも保持される(tmp_path):
    """計画期間は毎 run 描画する。さもないと Streamlit がウィジェット状態を
    掃除して、計画期間が既定値（7 日）へ戻ってしまう。

    ステップ 1 → 2 → 1 と往復しても日数が保持されることが条件。
    """
    at = _run_wizard(tmp_path, step=0)
    at.number_input(key="range_days").set_value(3).run()
    assert len(at.session_state[state.KEY_DAYS]) == 3
    at.button(key="wizard_next").click().run()
    assert len(at.session_state[state.KEY_DAYS]) == 3, "ステップ 2 でも計画期間が生きている"
    at.button(key="wizard_prev").click().run()
    assert at.number_input(key="range_days").value == 3


# --------------------------------------------------------------------------
# 3. 入力 → 確認 → 読み込み
# --------------------------------------------------------------------------


def _load_sample_into(at: object, kind: str) -> None:
    """ウィザードのサンプル読み込みの経路を実行する。"""
    at.radio(key=f"wizard_source_{kind}").set_value("🎲 サンプルを使う").run()
    at.button(key=f"wizard_sample_{kind}").click().run()


def test_サンプルで園児を入れて次のステップへ進める(tmp_path):
    at = _run_wizard(tmp_path, step=1)
    _load_sample_into(at, "children")
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(at.session_state["frame_children"]) > 0
    assert not at.button(key="wizard_next").disabled


def test_確認ステップで3表をまとめて読み込める(tmp_path):
    """園児・職員を入れて最後まで進み、読み込みで domain の型が session に入る。"""
    from shiftai.domain import ChildPlan, StaffMember

    at = _run_wizard(tmp_path, step=1)
    _load_sample_into(at, "children")
    at.button(key="wizard_next").click().run()
    assert "ステップ 3: 職員" in _markdown(at)
    _load_sample_into(at, "staff")
    at.button(key="wizard_next").click().run()
    at.button(key="wizard_next").click().run()
    assert "ステップ 5: 確認" in _markdown(at)

    apply_button = at.button(key="apply_load")
    assert not apply_button.disabled, "入力チェックが通っていれば押せる"
    apply_button.click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[state.KEY_LOAD_RESULT] is not None
    children = at.session_state[state.KEY_CHILDREN]
    staff = at.session_state[state.KEY_STAFF]
    assert children and all(isinstance(c, ChildPlan) for c in children)
    assert staff and all(isinstance(m, StaffMember) for m in staff)


def test_入力方法を切り替えても入力内容が消えない(tmp_path):
    """ウィザードで入れた園児データは「まとめて入力」に切り替えても残る。"""
    at = _run_app()
    at.button(key="wizard_start").click().run()
    at.button(key="wizard_next").click().run()
    _load_sample_into(at, "children")
    rows = len(at.session_state["frame_children"])

    at.radio(key=wizard.KEY_MODE).set_value(wizard.MODE_BULK).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert len(at.session_state["frame_children"]) == rows


# --------------------------------------------------------------------------
# 4. サイドバーとの衝突（DuplicateWidgetID 防止）
# --------------------------------------------------------------------------


def test_ウィザード時はサイドバーが期間と開所時刻を描かない():
    """同じキー（``range_start`` / ``day_open``）を二重に作らないこと。"""
    at = _run_app()
    at.button(key="wizard_start").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    labels = [w.label for w in at.date_input]
    assert "開始日" not in labels, "サイドバーの「開始日」が残っている"
    assert labels.count("計画期間の開始日") == 1
    assert [w.label for w in at.time_input].count("開所時刻") == 1


def test_まとめて入力時はサイドバーが期間と開所時刻を描く():
    """入力方法を戻すと、サイドバーが自分の項目を再び描くこと。"""
    at = _run_app()
    at.button(key="wizard_start").click().run()
    at.radio(key=wizard.KEY_MODE).set_value(wizard.MODE_BULK).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    labels = [w.label for w in at.date_input]
    assert "開始日" in labels
    assert "計画期間の開始日" not in labels
    assert [w.label for w in at.time_input].count("開所時刻") == 1


def _tmp():
    import tempfile

    return Path(tempfile.mkdtemp(prefix="shiftai_wizard_"))
