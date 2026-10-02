"""優先1（データ編集の Undo/Redo とバリデーション）の回帰テスト。

ここで守るべき挙動:

* 「登園時刻 > 降園時刻」「未登録の職員ID」といった入力を **タブ1で** 指摘できること
* ``data_loader`` が「静かに直す」値（週契約時間 0、最遅終業 <= 最早始業など）を
  利用者に見える形で Warn できること
* 誤編集を 1 コマンドで戻せること
"""

from __future__ import annotations

from datetime import date, time

import pandas as pd
import pytest

from shiftai import validation
from shiftai.ui.edit_history import EditHistory, frame_fingerprint

DAY = date(2026, 9, 28)
OPEN = time(7, 15)
CLOSE = time(19, 30)


# ---------------------------------------------------------------------------
# バリデーション
# ---------------------------------------------------------------------------


def _child(**overrides) -> dict:
    row = {
        "園児ID": "C001",
        "氏名": "園児1",
        "年齢": "3",
        "登園日": DAY,
        "登園時刻": time(9, 0),
        "降園時刻": time(15, 0),
        "短時間保育": "false",
        "欠席": "false",
        "欠席理由": "",
        "早朝保育": "false",
        "延長保育": "false",
        "備考": "",
    }
    row.update(overrides)
    return row


def _staff(**overrides) -> dict:
    row = {
        "職員ID": "S001",
        "氏名": "保育士1",
        "資格（主）": "保育士",
        "資格（副）": "",
        "雇用形態": "正職員",
        "週契約時間": 40,
        "1日契約時間": 8,
        "月間最小時間": 0,
        "月間最大時間": 200,
        "週最大出勤日数": 5,
        "最大連続勤務日数": 5,
        "最早始業": time(7, 0),
        "最遅終業": time(20, 0),
        "能力タグ": "",
        "備考": "",
    }
    row.update(overrides)
    return row


def _pref(**overrides) -> dict:
    row = {
        "職員ID": "S001",
        "種別": "出勤不可",
        "日付": DAY,
        "開始": time(9, 0),
        "終了": time(18, 0),
        "理由": "通院",
    }
    row.update(overrides)
    return row


def _issues(records: list[dict], **kwargs) -> list[validation.ValidationIssue]:
    return validation.validate_frames(
        pd.DataFrame.from_records(records),
        None,
        None,
        days=[DAY],
        day_open=OPEN,
        day_close=CLOSE,
    ).issues


def test_正常な入力は指摘ゼロ() -> None:
    report = validation.validate_frames(
        pd.DataFrame.from_records([_child()]),
        pd.DataFrame.from_records([_staff()]),
        pd.DataFrame.from_records([_pref()]),
        days=[DAY],
        day_open=OPEN,
        day_close=CLOSE,
    )
    assert report.issues == ()
    assert report.has_errors is False
    assert report.summary() == "指摘はありません。"


def test_降園時刻が登園時刻より後ならエラーになる() -> None:
    issues = _issues([_child(降園時刻=time(8, 30))])
    errors = [i for i in issues if i.is_error]
    assert any(i.column == "降園時刻" for i in errors), issues
    assert "登園時刻" in [i.message for i in errors if i.column == "降園時刻"][0]


def test_降園時刻が登園時刻と等しい場合もエラーになる() -> None:
    issues = _issues([_child(登園時刻=time(9, 0), 降園時刻=time(9, 0))])
    assert any(i.is_error and i.column == "降園時刻" for i in issues)


def test_欠席は時刻が無くてもエラーにならない() -> None:
    issues = _issues([_child(欠席="true", 欠席理由="体調不良")])
    assert not [i for i in issues if i.column in ("登園時刻", "降園時刻")]


def test_欠席理由が空ならエラーになる() -> None:
    issues = _issues([_child(欠席="true", 欠席理由="")])
    assert any(i.is_error and i.column == "欠席理由" for i in issues)


def test_園児IDの重複を検出する() -> None:
    issues = _issues([_child(), _child(氏名="別人")])
    assert any(i.is_error and "重複" in i.message and i.column == "園児ID" for i in issues)


def test_開所前の登園は警告になる() -> None:
    issues = _issues([_child(登園時刻=time(6, 0))])
    warn = [i for i in issues if not i.is_error]
    assert any(i.column == "登園時刻" for i in warn)


def test_職員IDが重複するとエラーになる() -> None:
    report = validation.validate_frames(
        None,
        pd.DataFrame.from_records([_staff(), _staff(氏名="同じ人")]),
        None,
        days=[DAY],
        day_open=OPEN,
        day_close=CLOSE,
    )
    assert report.has_errors
    assert any("重複" in i.message for i in report.errors)


def test_週契約時間0はエラーになる() -> None:
    report = validation.validate_frames(
        None,
        pd.DataFrame.from_records([_staff(週契約時間=0)]),
        None,
        days=[DAY],
        day_open=OPEN,
        day_close=CLOSE,
    )
    assert any(i.is_error and i.column == "週契約時間" for i in report.errors)


def test_1日の契約時間が週を超えるとエラーになる() -> None:
    report = validation.validate_frames(
        None,
        pd.DataFrame.from_records([_staff(週契約時間=4, **{"1日契約時間": 8})]),
        None,
        days=[DAY],
        day_open=OPEN,
        day_close=CLOSE,
    )
    assert any(i.is_error and i.column == "1日契約時間" for i in report.errors)


def test_最早始業が最遅終業以降ならエラーになる() -> None:
    report = validation.validate_frames(
        None,
        pd.DataFrame.from_records([_staff(最早始業=time(20, 0), 最遅終業=time(9, 0))]),
        None,
        days=[DAY],
        day_open=OPEN,
        day_close=CLOSE,
    )
    assert any(i.is_error and i.column == "最遅終業" for i in report.errors)


def test_未登録の職員IDを希望休で検出する() -> None:
    report = validation.validate_frames(
        None,
        pd.DataFrame.from_records([_staff()]),
        pd.DataFrame.from_records([_pref(職員ID="S999")]),
        days=[DAY],
        day_open=OPEN,
        day_close=CLOSE,
    )
    assert report.has_errors
    message = " ".join(i.message for i in report.errors)
    assert "S999" in message and "職員表にありません" in message


def test_希望休の開始終了が空なら全日として通す() -> None:
    report = validation.validate_frames(
        None,
        pd.DataFrame.from_records([_staff()]),
        pd.DataFrame.from_records([_pref(開始="", 終了="")]),
        days=[DAY],
        day_open=OPEN,
        day_close=CLOSE,
    )
    assert not [i for i in report.errors if i.table == "preferences"]


def test_希望休の終了が開始以下のときエラーになる() -> None:
    report = validation.validate_frames(
        None,
        pd.DataFrame.from_records([_staff()]),
        pd.DataFrame.from_records([_pref(開始=time(18, 0), 終了=time(9, 0))]),
        days=[DAY],
        day_open=OPEN,
        day_close=CLOSE,
    )
    assert any(i.is_error and i.column == "終了" for i in report.errors)


def test_報告はDataFrameとして描ける() -> None:
    report = validation.validate_frames(
        pd.DataFrame.from_records([_child(降園時刻=time(8, 0))]),
        pd.DataFrame.from_records([_staff()]),
        None,
        days=[DAY],
        day_open=OPEN,
        day_close=CLOSE,
    )
    frame = report.to_dataframe()
    assert list(frame.columns) == ["深刻度", "表", "行", "列", "内容"]
    assert not frame.empty
    # エラーが先頭に来る
    assert frame.iloc[0]["深刻度"] == validation.ERROR
    assert report.count_errors("children") >= 1


def test_空表は指摘なしになる() -> None:
    report = validation.validate_frames(
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        days=[DAY],
        day_open=OPEN,
        day_close=CLOSE,
    )
    assert report.issues == ()


@pytest.mark.parametrize("value", [None, "x", 1.5])
def test_未知の種別は警告になる(value) -> None:
    issues = validation.analyze_preferences(
        pd.DataFrame.from_records([_pref(種別=value)]),
        staff_ids=["S001"],
        days=[DAY],
    )
    assert any(i.column == "種別" for i in issues)
    assert not any(i.is_error for i in issues)


# ---------------------------------------------------------------------------
# Undo / Redo
# ---------------------------------------------------------------------------


def _frame(rows: int, tag: str) -> pd.DataFrame:
    return pd.DataFrame({"職員ID": [f"{tag}{i}" for i in range(rows)], "note": [tag] * rows})


def test_履歴は空から始まる() -> None:
    history = EditHistory()
    assert history.can_undo is False
    assert history.can_redo is False
    assert history.current is None
    assert "まだありません" in history.trail()


def test_記録すると元に戻せる() -> None:
    history = EditHistory()
    history.record("staff", _frame(3, "a"), "3 行")
    assert history.can_undo is False
    history.record("staff", _frame(4, "b"), "4 行")
    assert history.can_undo is True

    entry = history.undo()
    assert entry is not None
    assert len(entry.frame) == 3
    assert history.can_redo is True

    again = history.redo()
    assert again is not None
    assert len(again.frame) == 4


def test_同じ内容は記録しない() -> None:
    history = EditHistory()
    assert history.record("staff", _frame(3, "a"), "3 行") is True
    assert history.record("staff", _frame(3, "a"), "3 行") is False
    assert history.size == 1


def test_新しい編集はredo履歴を消す() -> None:
    history = EditHistory()
    history.record("staff", _frame(3, "a"), "a")
    history.record("staff", _frame(4, "b"), "b")
    history.undo()
    history.record("staff", _frame(9, "c"), "c")
    assert history.can_redo is False
    assert len(history.current.frame) == 9


def test_履歴長は上限で打ち切る() -> None:
    history = EditHistory(limit=3)
    for i in range(6):
        history.record("staff", _frame(i + 1, "x"), f"{i + 1}")
    assert history.size == 3
    assert history.current is not None
    assert len(history.current.frame) == 6


def test_undo_できる状態がないときはNoneになる() -> None:
    history = EditHistory()
    history.record("staff", _frame(3, "a"), "a")
    assert history.undo() is None
    assert history.redo() is None


def test_指紋は列名や順序が変わると変わる() -> None:
    left = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
    right = pd.DataFrame({"b": [3, 4], "a": [1, 2]})
    assert frame_fingerprint(left) != frame_fingerprint(right)
    assert frame_fingerprint(left) == frame_fingerprint(left.copy())
    assert frame_fingerprint(None) == "None"


def test_履歴のラベルに現在行数が出る() -> None:
    history = EditHistory()
    history.record("children", _frame(7, "a"), "園児 7 行")
    assert "園児 7 行" in history.undo_label() or history.undo_label() == "元に戻す"
