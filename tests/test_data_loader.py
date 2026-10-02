"""入力読込（``shiftai.data_loader``）のテスト。

「1 行壊れても全体が落ちない」こと、種々の表記ゆれを吸収できること、
壊れたデータで ``LoadResult.ok is False`` になり例外を投げないことを保証する。
"""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

import pandas as pd
import pytest

from shiftai.data_loader import (
    CHILDREN_COLUMNS,
    PREFERENCE_COLUMNS,
    STAFF_COLUMNS,
    LoadIssue,
    LoadResult,
    infer_columns,
    load_bundle,
    load_children,
    load_preferences,
    load_staff,
    normalize_header,
    parse_bool,
    parse_date,
    parse_role_list,
    parse_time,
    read_bundle,
    read_table,
    write_template_csvs,
)
from shiftai.domain import AgeClass, EmploymentType, Role

pytestmark = pytest.mark.timeout(300)

DAY = date(2026, 9, 28)


def _children_frame(rows):
    return pd.DataFrame.from_records(rows, columns=CHILDREN_COLUMNS)


def _staff_frame(rows):
    return pd.DataFrame.from_records(rows, columns=STAFF_COLUMNS)


# ---------------------------------------------------------------------------
# parse_date
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-28", date(2026, 9, 28)),
        ("2026/9/28", date(2026, 9, 28)),
        ("2026.09.28", date(2026, 9, 28)),
        ("2026年9月28日", date(2026, 9, 28)),
        ("20260928", date(2026, 9, 28)),
        (date(2026, 9, 28), date(2026, 9, 28)),
        (pd.Timestamp("2026-09-28"), date(2026, 9, 28)),
    ],
)
def test_parse_date_通常系(value, expected):
    """ISO・和風・Excel 由来の日付表記を吸収すること。"""
    assert parse_date(value) == expected


@pytest.mark.parametrize("value", ["", None, "不明", "hello", float("nan")])
def test_parse_date_判定不能はNone(value):
    """日付として読めない値は ``None``（例外ではない）になること。"""
    assert parse_date(value) is None


def test_parse_date_Excelのシリアル値():
    """Excel のシリアル値（1 以上の数値）を日付に変換できること。"""
    assert parse_date(46293) == date(2026, 9, 28)


# ---------------------------------------------------------------------------
# parse_time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("08:00", time(8, 0)),
        ("8:00", time(8, 0)),
        ("8時", time(8, 0)),
        ("8時30分", time(8, 30)),
        ("08時30分", time(8, 30)),
        ("0830", time(8, 30)),
        (time(8, 30), time(8, 30)),
        (pd.Timestamp("2026-09-28 08:30:00"), time(8, 30)),
    ],
)
def test_parse_time_通常系(value, expected):
    """``8時`` や ``08:00`` などの表記ゆれを吸収すること。"""
    assert parse_time(value) == expected


@pytest.mark.parametrize("value", ["25:00", "24:00", "-1:00", "8:99", "夜", None, ""])
def test_parse_time_範囲外はNone(value):
    """存在しない時刻（25:00 など）は ``None`` になること。"""
    assert parse_time(value) is None


def test_parse_time_Excelの分数():
    """1 未満の数値は「日の内割合」として解釈されること。"""
    assert parse_time(0.5) == time(12, 0)


# ---------------------------------------------------------------------------
# parse_bool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["○", "◯", "はい", "有", "true", "TRUE", "1", "y", True, 1])
def test_parse_bool_真(value):
    """真とみなされる表記ゆれを吸収すること。"""
    assert parse_bool(value) is True


@pytest.mark.parametrize("value", ["×", "いいえ", "なし", "false", "0", "n", False, 0])
def test_parse_bool_偽(value):
    """偽とみなされる表記ゆれを吸収すること。"""
    assert parse_bool(value) is False


def test_parse_bool_判定不能はdefault():
    """判定不能な値は ``default`` を返す（例外ではない）こと。"""
    assert parse_bool("たぶん", default=True) is True
    assert parse_bool("たぶん", default=False) is False
    assert parse_bool(None, default=True) is True
    assert parse_bool("", default=True) is True


# ---------------------------------------------------------------------------
# parse_role_list
# ---------------------------------------------------------------------------


def test_parse_role_list_複数():
    """縦棒・読点・スラッシュ区切りを複数資格として扱うこと。"""
    assert parse_role_list("保育士|看護師") == (Role.HOIKUSHI, Role.KANGSHI)
    assert parse_role_list("保育士、看護師") == (Role.HOIKUSHI, Role.KANGSHI)
    assert parse_role_list("保育士/幼稚園教諭") == (Role.HOIKUSHI, Role.YOUCHUIN)


def test_parse_role_list_重複を除去():
    """同じ資格が重複しても 1 件にまとめること。"""
    assert parse_role_list("保育士|保育士") == (Role.HOIKUSHI,)


def test_parse_role_list_空と未知():
    """空文字は空タプル、未知の資格は無視すること。"""
    assert parse_role_list("") == ()
    assert parse_role_list(None) == ()
    assert parse_role_list("存在しない資格") == ()


# ---------------------------------------------------------------------------
# ヘッダ正規化・列推定
# ---------------------------------------------------------------------------


def test_normalize_header():
    """空白・括弧・記号を落としてヘッダを正規化すること。"""
    assert normalize_header("園児 ID") == normalize_header("園児ID").lower()
    assert normalize_header("  姓　名  ") == "姓名"
    assert normalize_header("登園時刻(任意)") == "登園時刻任意"
    assert normalize_header("園児 ID") != normalize_header("園児名")


def test_infer_columns_ヘッダゆれを吸収():
    """微妙に違うヘッダを期待列名へ割り当てられること。"""
    frame = pd.DataFrame(
        [{"園児 ID": "C001", "氏名 ": "テスト", "年齢": "2", "登園日": "2026-09-28"}]
    )
    mapping = infer_columns(frame, CHILDREN_COLUMNS)
    assert mapping["園児 ID"] == "園児ID"
    assert mapping["氏名 "] == "氏名"
    assert mapping["年齢"] == "年齢"
    assert mapping["登園日"] == "登園日"


def test_infer_columns_空は空dict():
    """列がない DataFrame では空の対応表になること。"""
    assert infer_columns(pd.DataFrame(), CHILDREN_COLUMNS) == {}


# ---------------------------------------------------------------------------
# load_children
# ---------------------------------------------------------------------------


def test_load_children_正常系():
    """正常な CSV は ChildPlan のリストになること。"""
    frame = _children_frame(
        [
            {
                "園児ID": "C001",
                "氏名": "佐藤 さくら",
                "年齢": "2",
                "登園日": "2026-09-28",
                "登園時刻": "08:30",
                "降園時刻": "17:00",
            },
            {
                "園児ID": "C002",
                "氏名": "鈴木 ひなた",
                "年齢": "0",
                "登園日": "2026-09-28",
                "登園時刻": "9時",
                "降園時刻": "17時15分",
                "短時間保育": "○",
                "延長保育": "はい",
            },
        ]
    )
    children, issues = load_children(frame)
    assert len(children) == 2
    assert {c.child_id for c in children} == {"C001", "C002"}
    assert not [i for i in issues if i.level == "error"]
    short = next(c for c in children if c.child_id == "C002")
    assert short.age_class is AgeClass.INFANT
    assert short.is_short_time is True
    assert short.uses_late_care is True
    assert short.arrive == time(9, 0)
    assert short.depart == time(17, 15)


def test_load_children_欠席は在園0():
    """欠席の園児は在園時間 0 として読まれること。"""
    frame = _children_frame(
        [
            {
                "園児ID": "C001",
                "氏名": "欠席",
                "年齢": "3",
                "登園日": "2026-09-28",
                "登園時刻": "",
                "降園時刻": "",
                "欠席": "true",
                "欠席理由": "発熱",
            }
        ]
    )
    children, _ = load_children(frame)
    assert children[0].absent is True
    assert children[0].stay_minutes == 0


def test_load_children_壊れた行はerrorになる():
    """年齢も登園日も読めない行は ``error`` として積み上げ、致命的にならないこと。"""
    frame = _children_frame(
        [
            {
                "園児ID": "C001",
                "氏名": "有効",
                "年齢": "3",
                "登園日": "2026-09-28",
                "登園時刻": "08:30",
                "降園時刻": "17:00",
            },
            {
                "園児ID": "C002",
                "氏名": "壊れ",
                "年齢": "不明",
                "登園日": "不明",
                "登園時刻": "",
                "降園時刻": "",
            },
        ]
    )
    children, issues = load_children(frame)
    assert len(children) == 1
    assert [i for i in issues if i.level == "error"]


def test_load_children_空は空リスト():
    """0 行の DataFrame でも例外を投げないこと。"""
    children, issues = load_children(pd.DataFrame(columns=CHILDREN_COLUMNS))
    assert children == []
    assert issues == []


# ---------------------------------------------------------------------------
# load_staff
# ---------------------------------------------------------------------------


def test_load_staff_正常系():
    """職員 CSV が StaffMember のリストになること。"""
    frame = _staff_frame(
        [
            {
                "職員ID": "S001",
                "氏名": "山田花子",
                "資格（主）": "保育士",
                "資格（副）": "",
                "雇用形態": "正職員",
                "週契約時間": "40",
                "1日契約時間": "8",
                "最早始業": "07:00",
                "最遅終業": "20:00",
            },
            {
                "職員ID": "S002",
                "氏名": "鈴木由美",
                "資格（主）": "子育て支援員",
                "雇用形態": "パート",
                "週契約時間": "20",
                "1日契約時間": "5",
            },
        ]
    )
    staff, issues = load_staff(frame)
    assert len(staff) == 2
    assert not [i for i in issues if i.level == "error"]
    first = staff[0]
    assert first.has_role(Role.HOIKUSHI)
    assert not first.has_role(Role.KANGSHI)
    assert first.contract.employment_type is EmploymentType.SEI
    assert first.contract.earliest_start == time(7, 0)
    assert first.contract.latest_end == time(20, 0)
    assert staff[1].contract.employment_type is EmploymentType.PART


def test_load_staff_資格が空はerror():
    """資格が空の職員は読めず、他行はBOROUGH処理されること。"""
    frame = _staff_frame(
        [
            {
                "職員ID": "S001",
                "氏名": "有資格",
                "資格（主）": "保育士",
                "週契約時間": "40",
                "1日契約時間": "8",
            },
            {
                "職員ID": "S002",
                "氏名": "無資格",
                "資格（主）": "",
                "週契約時間": "40",
                "1日契約時間": "8",
            },
        ]
    )
    staff, issues = load_staff(frame)
    assert [s.staff_id for s in staff] == ["S001"]
    assert [i for i in issues if i.level == "error"]


def test_load_staff_職員ID重複はerror():
    """職員IDが重複したら 2 行目を ``error`` として落とし、先の行を残すこと。"""
    frame = _staff_frame(
        [
            {
                "職員ID": "S001",
                "氏名": "A",
                "資格（主）": "保育士",
                "週契約時間": "40",
                "1日契約時間": "8",
            },
            {
                "職員ID": "S001",
                "氏名": "B",
                "資格（主）": "保育士",
                "週契約時間": "40",
                "1日契約時間": "8",
            },
        ]
    )
    staff, issues = load_staff(frame)
    assert [s.staff_id for s in staff] == ["S001"]
    assert any(i.level == "error" and "重複" in i.message for i in issues)


# ---------------------------------------------------------------------------
# load_preferences
# ---------------------------------------------------------------------------


def test_load_preferences_希望休():
    """希望休 CSV が StaffPreferences の辞書になること。"""
    frame = pd.DataFrame.from_records(
        [
            {
                "職員ID": "S001",
                "種別": "出勤不可",
                "日付": "2026-09-28",
                "開始": "",
                "終了": "",
                "理由": "希望休",
            },
            {
                "職員ID": "S001",
                "種別": "出勤希望",
                "日付": "2026-09-29",
                "開始": "",
                "終了": "",
                "理由": "",
            },
            {
                "職員ID": "S002",
                "種別": "休み希望",
                "日付": "2026-09-30",
                "開始": "",
                "終了": "",
                "理由": "",
            },
        ],
        columns=PREFERENCE_COLUMNS,
    )
    prefs, issues = load_preferences(frame)
    assert set(prefs) == {"S001", "S002"}
    assert not [i for i in issues if i.level == "error"]
    assert prefs["S001"].unavailable_days() == {date(2026, 9, 28)}
    assert prefs["S001"].preferred_days == frozenset({date(2026, 9, 29)})
    assert prefs["S002"].preferred_off_days == frozenset({date(2026, 9, 30)})


def test_load_preferences_時間帯つき():
    """時間帯つきの不可区間も読めること。"""
    frame = pd.DataFrame.from_records(
        [
            {
                "職員ID": "S001",
                "種別": "出勤不可",
                "日付": "2026-09-28",
                "開始": "13:00",
                "終了": "14:00",
                "理由": "通院",
            }
        ],
        columns=PREFERENCE_COLUMNS,
    )
    prefs, _ = load_preferences(frame)
    assert prefs["S001"].unavailable[0].start == time(13, 0)
    assert prefs["S001"].unavailable[0].end == time(14, 0)


def test_load_preferences_空は空dict():
    """0 行なら空辞書になること。"""
    prefs, issues = load_preferences(pd.DataFrame(columns=PREFERENCE_COLUMNS))
    assert prefs == {}
    assert issues == []


# ---------------------------------------------------------------------------
# load_bundle / LoadResult
# ---------------------------------------------------------------------------


def test_load_bundle_正常系():
    """3 枚まとめて読めて ``ok=True`` になること。"""
    result = load_bundle(
        _children_frame(
            [
                {
                    "園児ID": "C001",
                    "氏名": "A",
                    "年齢": "3",
                    "登園日": "2026-09-28",
                    "登園時刻": "08:30",
                    "降園時刻": "17:00",
                }
            ]
        ),
        _staff_frame(
            [
                {
                    "職員ID": "S001",
                    "氏名": "B",
                    "資格（主）": "保育士",
                    "週契約時間": "40",
                    "1日契約時間": "8",
                }
            ]
        ),
        pd.DataFrame.from_records(
            [
                {
                    "職員ID": "S001",
                    "種別": "出勤不可",
                    "日付": "2026-09-28",
                    "開始": "",
                    "終了": "",
                    "理由": "希望休",
                }
            ],
            columns=PREFERENCE_COLUMNS,
        ),
    )
    assert result.ok is True
    assert len(result.children) == 1
    assert len(result.staff) == 1
    assert set(result.preferences) == {"S001"}


def test_load_bundle_壊れたデータでokがFalse():
    """壊れたデータでも例外を投げず ``ok is False`` になりえること。"""
    result = load_bundle(
        _children_frame(
            [
                {
                    "園児ID": "C001",
                    "氏名": "A",
                    "年齢": "不明",
                    "登園日": "不明",
                    "登園時刻": "",
                    "降園時刻": "",
                },
            ]
        ),
        _staff_frame(
            [{"職員ID": "S001", "氏名": "", "資格（主）": "", "週契約時間": "", "1日契約時間": ""}]
        ),
    )
    assert result.ok is False
    assert result.errors
    assert isinstance(result.summary(), str)


def test_load_bundle_職員にいない希望休はwarning():
    """職員 CSV にいない職員 ID の希望休は error ではなく warning にすること。"""
    result = load_bundle(
        None,
        _staff_frame(
            [
                {
                    "職員ID": "S001",
                    "氏名": "B",
                    "資格（主）": "保育士",
                    "週契約時間": "40",
                    "1日契約時間": "8",
                }
            ]
        ),
        pd.DataFrame.from_records(
            [
                {
                    "職員ID": "S999",
                    "種別": "出勤不可",
                    "日付": "2026-09-28",
                    "開始": "",
                    "終了": "",
                    "理由": "希望休",
                }
            ],
            columns=PREFERENCE_COLUMNS,
        ),
    )
    assert result.ok is True
    assert result.warnings
    assert "S999" in result.summary()


def test_load_bundle_何も渡さない():
    """全部 ``None`` でも空の結果が返ること。"""
    result = load_bundle()
    assert result.ok is True
    assert result.children == [] and result.staff == [] and result.preferences == {}


def test_load_result_プロパティ():
    """``LoadResult`` の errors/warnings/ok/summary が整合すること。"""
    result = LoadResult()
    result.issues.append(LoadIssue("error", 1, "園児ID", "壊れています"))
    result.issues.append(LoadIssue("warning", 2, "氏名", "空欄です"))
    assert result.ok is False
    assert len(result.errors) == 1
    assert len(result.warnings) == 1
    assert "壊れています" in result.summary()
    assert "第2行" in str(result.issues[0])


def test_load_bundle_種別が未知ならwarningして希望休扱い():
    """未知の種別は ``warning`` を出して「希望休」として扱うこと。"""
    result = load_bundle(
        None,
        _staff_frame(
            [
                {
                    "職員ID": "S001",
                    "氏名": "B",
                    "資格（主）": "保育士",
                    "週契約時間": "40",
                    "1日契約時間": "8",
                }
            ]
        ),
        pd.DataFrame.from_records(
            [
                {
                    "職員ID": "S001",
                    "種別": "その他",
                    "日付": "2026-09-28",
                    "開始": "",
                    "終了": "",
                    "理由": "早朝を避けたい",
                }
            ],
            columns=PREFERENCE_COLUMNS,
        ),
    )
    assert result.preferences["S001"].unavailable_days() == {date(2026, 9, 28)}
    assert any("未知の種別" in i.message for i in result.warnings)


def test_load_staff_副資格がある():
    """副資格（資格（副））がある職員も例外なく読め、roles に統合されること。

    以前は ``parse_role_list`` が返す tuple に対して ``roles.append`` していて
    AttributeError になり、「資格（副）」のある行を持つ CSV 全体が読めなかった。
    ``load_staff`` 側を ``list(...)`` にコピーする実装へ修正済み。
    """
    frame = _staff_frame(
        [
            {
                "職員ID": "S001",
                "氏名": "山田花子",
                "資格（主）": "保育士",
                "資格（副）": "看護師",
                "週契約時間": "40",
                "1日契約時間": "8",
            }
        ]
    )
    staff, issues = load_staff(frame)
    assert len(staff) == 1
    assert staff[0].has_role(Role.KANGSHI)


# ---------------------------------------------------------------------------
# read_table / read_bundle
# ---------------------------------------------------------------------------


def test_read_table_DataFrameをそのまま返す():
    """DataFrame を渡すとコピーが返ること。"""
    frame = _children_frame([])
    out = read_table(frame, "children")
    assert list(out.columns) == CHILDREN_COLUMNS
    assert out is not frame


def test_read_table_bytes():
    """CSV の bytes を渡すと DataFrame になること。"""
    payload = "園児ID,氏名,年齢,登園日,登園時刻,降園時刻\nC001,佐藤,3,2026-09-28,08:30,17:00\n"
    out = read_table(payload.encode("utf-8"), "children")
    assert out.loc[0, "園児ID"] == "C001"


def test_read_table_Path(tmp_path: Path):
    """ファイルパスを渡すと DataFrame になること。"""
    path = tmp_path / "children.csv"
    path.write_text(
        "園児ID,氏名,年齢,登園日,登園時刻,降園時刻\nC001,佐藤,3,2026-09-28,08:30,17:00\n",
        encoding="utf-8-sig",
    )
    out = read_table(path, "children")
    assert out.loc[0, "氏名"] == "佐藤"
    assert read_table(str(path), "children").loc[0, "園児ID"] == "C001"


def test_read_table_Excel(tmp_path: Path):
    """``.xlsx`` のパスを読めること（openpyxl 経由）。"""
    path = tmp_path / "staff.xlsx"
    _staff_frame(
        [
            {
                "職員ID": "S001",
                "氏名": "B",
                "資格（主）": "保育士",
                "週契約時間": "40",
                "1日契約時間": "8",
            }
        ]
    ).to_excel(path, index=False)
    out = read_table(path, "staff")
    assert out.loc[0, "職員ID"] == "S001"


def test_read_table_文字列のCSV():
    """パスでない CSV テキストも解釈できること。"""
    out = read_table(
        "園児ID,氏名,年齢,登園日,登園時刻,降園時刻\nC001,佐藤,3,2026-09-28,08:30,17:00\n",
        "children",
    )
    assert len(out) == 1


def test_read_table_JSON():
    """JSON の配列を読めること（GAS 連携の受け皿）。"""
    out = read_table('[{"園児ID": "C001", "氏名": "佐藤"}]', "children")
    assert out.loc[0, "園児ID"] == "C001"


def test_read_table_空bytesは空DataFrame():
    """空バイト列では期待列のみの空 DataFrame が返ること。"""
    out = read_table(b"", "children")
    assert out.empty
    assert list(out.columns) == CHILDREN_COLUMNS


def test_read_table_不正なkind():
    """未知の ``kind`` は ``ValueError`` にすること。"""
    with pytest.raises(ValueError):
        read_table(b"a,b\n1,2", "unknown")


def test_read_table_読めない型():
    """扱えない型は ``ValueError`` にすること。"""
    with pytest.raises(ValueError):
        read_table(object(), "children")


def test_read_table_壊れたJSON():
    """壊れた JSON は ``ValueError`` になること。"""
    with pytest.raises(ValueError):
        read_table("{これはJSONではない", "children")


def test_read_bundle():
    """``read_bundle`` が bytes 3 枚から LoadResult を作ること。"""
    result = read_bundle(
        "園児ID,氏名,年齢,登園日,登園時刻,降園時刻\nC001,佐藤,3,2026-09-28,08:30,17:00\n",
        "職員ID,氏名,資格（主）,週契約時間,1日契約時間\nS001,山田,保育士,40,8\n",
        None,
    )
    assert result.ok is True
    assert len(result.children) == 1
    assert len(result.staff) == 1


# ---------------------------------------------------------------------------
# テンプレート出力
# ---------------------------------------------------------------------------


def test_write_template_csvs(tmp_path: Path):
    """テンプレート 3 枚が一時ディレクトリに書き出され、再読込できること。"""
    out = write_template_csvs(tmp_path / "templates")
    assert set(out) == {"children", "staff", "preferences"}
    for path in out.values():
        assert path.exists()
        assert path.stat().st_size > 0
    children = read_table(out["children"], "children")
    assert len(children) == 1
    assert list(children.columns) == CHILDREN_COLUMNS
    staff = read_table(out["staff"], "staff")
    assert len(staff) == 1
    assert list(staff.columns) == STAFF_COLUMNS


def test_write_template_csvs_既存ディレクトリでも動く(tmp_path: Path):
    """既に存在しても上書きして例外にしないこと。"""
    target = tmp_path / "templates"
    write_template_csvs(target)
    out = write_template_csvs(target)
    assert all(p.exists() for p in out.values())


# ---------------------------------------------------------------------------
# 出勤日数の上限（T-03）
# ---------------------------------------------------------------------------


def _cap_staff_frame(**overrides):
    """職員1行分の DataFrame を作る。"""
    row = {
        "職員ID": "S001",
        "氏名": "山田",
        "資格（主）": "保育士",
        "雇用形態": "正職員",
        "週契約時間": "40",
        "1日契約時間": "8",
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_週最大出勤日数が0なら0のまま残る():
    """``0`` は「週あたりの上限なし」なので 5 に丸めずそのまま保持すること。

    修正前は ``_parse_int(..., 5) or 5`` のため 0 が 5 になっていた。
    ソルバ側は ``max_weekly_days > 0`` を「上限あり」の判定に使っており、
    読み込み層だけがその意味を打ち消していた。
    """
    members, _ = load_staff(_cap_staff_frame(週最大出勤日数=0, 最大連続勤務日数=0))
    assert members[0].contract.max_weekly_days == 0
    assert members[0].contract.max_consecutive_days == 0


def test_列が無ければ既定5になる():
    """列自体が無い場合は従来どおり 5 日になること。"""
    members, _ = load_staff(_cap_staff_frame())
    assert members[0].contract.max_weekly_days == 5
    assert members[0].contract.max_consecutive_days == 5


def test_空文字と欠損なら既定5になる():
    """空文字と欠損値（NaN）も「未入力」として 5 日にすること。"""
    for value in ("", None, "   "):
        members, _ = load_staff(_cap_staff_frame(週最大出勤日数=value))
        assert members[0].contract.max_weekly_days == 5, value


def test_指定された値はそのまま反映される():
    """0 以外の値は変更されずに反映されること。"""
    members, _ = load_staff(_cap_staff_frame(週最大出勤日数=3, 最大連続勤務日数=4))
    assert members[0].contract.max_weekly_days == 3
    assert members[0].contract.max_consecutive_days == 4


def test_負値はエラーとして積み上げられる():
    """負値は行を黙って捨てずにエラーとして記録すること。"""
    members, issues = load_staff(_cap_staff_frame(週最大出勤日数=-1))
    assert members == []
    errors = [i for i in issues if i.level == "error"]
    assert errors, "エラーとして記録されること"
    assert "週最大出勤日数" in str(errors[0])


def test_副資格が複数あっても例外にならない():
    """資格（副）に複数の資格が入っても例外にならないこと。

    修正前は ``roles.append(extra)`` をタプルに対して行っており
    ``AttributeError`` で読み込み全体が落ちていた。
    """
    frame = _cap_staff_frame()
    frame["資格（副）"] = "看護師|栄養教諭"
    members, issues = load_staff(frame)
    assert [i.level for i in issues if i.level == "error"] == []
    assert members[0].roles == (Role.HOIKUSHI, Role.KANGSHI, Role.EIYOU)
