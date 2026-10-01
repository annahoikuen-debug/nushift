"""園業務支援システム CSV の取込アダプター（:mod:`shiftai.importers`）のテスト。"""

from __future__ import annotations

from datetime import date, time

import pandas as pd

from shiftai import importers
from shiftai.data_loader import CHILDREN_COLUMNS, load_children


def _codemon_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "園児コード": "A001",
                "園児氏名": "山田 太郎",
                "年齢": "3",
                "利用日": "2026/9/28",
                "登園予定時刻": "8:30",
                "降園予定時刻": "16:30",
                "欠席": "なし",
            }
        ]
    )


def _kids_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "園児ID": "K001",
                "氏名": "鈴木 花子",
                "歳": 4,
                "登園日": "2026-09-28",
                "登園予定時刻": "9:00",
                "降園予定時刻": "17:00",
                "延長保育利用": "○",
            }
        ]
    )


# ---------------------------------------------------------------------------
# プロファイル
# ---------------------------------------------------------------------------


def test_プロファイルが3種並ぶ():
    keys = [p.key for p in importers.list_profiles()]
    assert keys == ["codemon", "kids_ryu", "generic"]


def test_選択肢がselectboxで使える形():
    options = importers.profile_options()
    assert all(isinstance(key, str) and isinstance(label, str) for key, label in options)


def test_未知のキーはgenericへ落ちる():
    assert importers.get_profile("unknown").key == "generic"


# ---------------------------------------------------------------------------
# 自動判定
# ---------------------------------------------------------------------------


def test_CoDMONの列名からcodemonを判定する():
    assert importers.detect_profile(_codemon_frame()) == "codemon"


def test_キッズリーの列名からkids_ryuを判定する():
    assert importers.detect_profile(_kids_frame()) == "kids_ryu"


def test_判定できない場合はgeneric():
    frame = pd.DataFrame([{"X": 1, "Y": 2}])
    assert importers.detect_profile(frame) == "generic"


def test_空表はgenericになる():
    assert importers.detect_profile(pd.DataFrame()) == "generic"


# ---------------------------------------------------------------------------
# 変換
# ---------------------------------------------------------------------------


def test_CoDMON的行が本アプリ形式になる():
    result = importers.convert(_codemon_frame(), "codemon")
    assert list(result.frame.columns) == CHILDREN_COLUMNS
    row = result.frame.iloc[0]
    assert row["園児ID"] == "A001"
    assert row["氏名"] == "山田 太郎"
    assert row["年齢"] == "3"
    assert row["登園日"] == date(2026, 9, 28)
    assert row["登園時刻"] == time(8, 30)
    assert row["降園時刻"] == time(16, 30)
    assert row["欠席"] == "false"


def test_キッズリーの行が本アプリ形式になる():
    result = importers.convert(_kids_frame(), "kids_ryu")
    row = result.frame.iloc[0]
    assert row["園児ID"] == "K001"
    assert row["年齢"] == "4"
    assert row["登園日"] == date(2026, 9, 28)
    assert row["延長保育"] == "true"


def test_変換結果はそのままload_childrenへ渡せる():
    result = importers.convert(_codemon_frame(), "codemon")
    children, issues = load_children(result.frame)
    assert not issues
    assert len(children) == 1
    assert children[0].child_id == "A001"


def test_対応する列が無ければmissingに列挙される():
    frame = pd.DataFrame([{"園児コード": "A001"}])
    result = importers.convert(frame, "codemon")
    assert "登園時刻" in result.missing
    assert result.mapping["園児ID"] == "園児コード"


def test_対応表が本アプリの全列を並べる():
    result = importers.convert(_codemon_frame(), "codemon")
    frame = importers.mapping_frame(result)
    assert list(frame["本アプリの列"]) == CHILDREN_COLUMNS
    assert frame.iloc[0]["取り込んだ列"] == "園児コード"


def test_空表は空の変換結果を返す():
    result = importers.convert(pd.DataFrame(), "codemon")
    assert result.frame.empty
    assert list(result.frame.columns) == CHILDREN_COLUMNS


# ---------------------------------------------------------------------------
# 値の正規化
# ---------------------------------------------------------------------------


def test_和暦の日付を西暦に変換する():
    frame = pd.DataFrame(
        [
            {
                "園児コード": "A001",
                "園児氏名": "A",
                "年齢": "3",
                "利用日": "R8.9.28",
                "登園予定時刻": "8:30",
                "降園予定時刻": "16:30",
            }
        ]
    )
    result = importers.convert(frame, "codemon")
    assert result.frame.iloc[0]["登園日"] == date(2026, 9, 28)


def test_年齢が6以上でも5に丸められる():
    frame = _codemon_frame()
    frame.loc[0, "年齢"] = "7"
    result = importers.convert(frame, "codemon")
    assert result.frame.iloc[0]["年齢"] == "5"


def test_月齢の表記は0歳児になる():
    frame = _codemon_frame()
    frame.loc[0, "年齢"] = "11ヶ月"
    result = importers.convert(frame, "codemon")
    assert result.frame.iloc[0]["年齢"] == "0"


def test_欠席は理由が無くても読み込める():
    frame = _codemon_frame()
    frame.loc[0, "欠席"] = "あり"
    result = importers.convert(frame, "codemon")
    assert result.frame.iloc[0]["欠席"] == "true"


def test_短時間保育は保育標準時間帯と一致するなら推定する():
    frame = pd.DataFrame(
        [
            {
                "園児コード": "A001",
                "園児氏名": "A",
                "年齢": "4",
                "利用日": "2026/9/28",
                "登園予定時刻": "9:00",
                "降園予定時刻": "14:00",
            }
        ]
    )
    result = importers.convert(
        frame, "codemon", standard_time=(time(9, 0), time(14, 0))
    )
    assert result.frame.iloc[0]["短時間保育"] == "true"


def test_保育標準時間帯と違えば短時間保育にしない():
    frame = _codemon_frame()
    result = importers.convert(
        frame, "codemon", standard_time=(time(9, 0), time(14, 0))
    )
    assert result.frame.iloc[0]["短時間保育"] == "false"


def test_短時間保育のフラグがあれば推定より優先する():
    frame = pd.DataFrame(
        [
            {
                "園児コード": "A001",
                "園児氏名": "A",
                "年齢": "4",
                "利用日": "2026/9/28",
                "登園予定時刻": "9:00",
                "降園予定時刻": "14:00",
                "短時間保育": "なし",
            }
        ]
    )
    result = importers.convert(
        frame, "codemon", standard_time=(time(9, 0), time(14, 0))
    )
    assert result.frame.iloc[0]["短時間保育"] == "false"


def test_全角や空白を含む列名でも読み込める():
    frame = pd.DataFrame(
        [
            {
                "園児コード ": "A001",
                " 園児氏名": "A",
                "年齢": "3",
                "利用日": "2026/9/28",
                "登園予定時刻": "8:30",
                "降園予定時刻": "16:30",
            }
        ]
    )
    result = importers.convert(frame, "codemon")
    assert result.frame.iloc[0]["園児ID"] == "A001"


def test_注意文がプロファイルについて来る():
    result = importers.convert(_codemon_frame(), "codemon")
    assert result.notes


def test_欠損値があっても落ちない():
    frame = pd.DataFrame(
        [{"園児コード": None, "園児氏名": None, "利用日": None}]
    )
    result = importers.convert(frame, "codemon")
    assert len(result.frame) == 1
    assert result.frame.iloc[0]["園児ID"] == ""