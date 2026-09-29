"""出力（``shiftai.exporter``）のテスト。

CSV（UTF-8 BOM）/ Excel（.xlsx）/ iCalendar / ZIP / マークダウンサマリーが
「 실제로開ける」「形式が正しい」ことを保証する。
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date

import pandas as pd
import pytest
from openpyxl import load_workbook

from shiftai import local_rules
from shiftai.domain import CellState, FacilitySettings, RequirementTable
from shiftai.exporter import (
    BOM,
    export_bundle_zip,
    payroll_dataframe,
    requirements_dataframe,
    shift_matrices,
    shift_matrix_dataframe,
    shift_to_dataframe,
    summary_markdown,
    to_csv_bytes,
    to_excel_bytes,
    to_ics,
)
from shiftai.solver import solve_shift_greedy
from tests.conftest import DAY, DAY_CLOSE, DAY_OPEN, STANDARD_KEY, part_contract, sei_contract

pytestmark = pytest.mark.timeout(300)

STANDARD = local_rules.get_standard(STANDARD_KEY)


@pytest.fixture(scope="module")
def solved(small_children, small_staff, small_requirements):
    """貪欲法で作った小さな解（エクスポートのテストには最適解である必要なし）。"""
    return solve_shift_greedy(
        small_children, small_staff, small_requirements, standard=STANDARD
    )


@pytest.fixture(scope="module")
def slots_of(small_requirements):
    return tuple(small_requirements.slots)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def test_to_csv_bytesはBOM付き():
    """Excel で開いたiquid時に文字化けしないよう BOM が付くこと。"""
    frame = pd.DataFrame({"職員ID": ["S001"], "氏名": ["山田花子"]})
    data = to_csv_bytes(frame)
    assert isinstance(data, bytes)
    assert data.startswith(BOM.encode("utf-8"))
    assert b"\xef\xbb\xbf" == data[:3]
    text = data.decode("utf-8-sig")
    assert "S001" in text and "山田花子" in text


def test_to_csv_bytesはBOM無効化できる():
    """``bom=False`` で素の UTF-8 になること。"""
    frame = pd.DataFrame({"a": [1]})
    data = to_csv_bytes(frame, bom=False)
    assert not data.startswith(b"\xef\xbb\xbf")
    assert data.decode("utf-8").splitlines()[0] == "a"


def test_to_csv_bytesはindexを落とせる():
    """``index=False``（既定）でインデックス列が出ないこと。"""
    frame = pd.DataFrame({"a": [1, 2]})
    assert to_csv_bytes(frame).decode("utf-8-sig").splitlines()[0] == "a"
    assert "," in to_csv_bytes(frame, index=True).decode("utf-8-sig").splitlines()[0]


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------


def test_to_excel_bytesはopenpyxlで読める():
    """生成バイト列を openpyxl で開けてシート名・値が一致すること。"""
    sheets = {
        "シフト": pd.DataFrame({"職員ID": ["S001"], "状態": ["勤務"]}),
        "給与計算": pd.DataFrame({"職員ID": ["S001"], "実働時間": [8.0]}),
    }
    data = to_excel_bytes(sheets)
    assert data[:2] == b"PK"
    workbook = load_workbook(io.BytesIO(data))
    assert workbook.sheetnames == ["シフト", "給与計算"]
    assert workbook["シフト"]["A1"].value == "職員ID"
    assert workbook["給与計算"]["B2"].value == pytest.approx(8.0)


def test_to_excel_bytesは不正なシート名を安全な名前にする():
    """31 文字超や禁止文字を含むシート名でも Excel が壊れないこと。"""
    long_name = "あ" * 40
    data = to_excel_bytes({long_name: pd.DataFrame({"a": [1]}), "A/B:C": pd.DataFrame({"a": [1]})})
    workbook = load_workbook(io.BytesIO(data))
    assert all(len(name) <= 31 for name in workbook.sheetnames)
    assert all(not set(name) & set("[]:*?/\\") for name in workbook.sheetnames)
    assert len(workbook.sheetnames) == 2


def test_to_excel_bytesは同じ名前で書ける():
    """シート名が重複しても連番で振り直されること。"""
    data = to_excel_bytes({"シフト": pd.DataFrame({"a": [1]}), " シフト": pd.DataFrame({"a": [2]})})
    workbook = load_workbook(io.BytesIO(data))
    assert len(workbook.sheetnames) == 2
    assert len(set(workbook.sheetnames)) == 2


def test_to_excel_bytesは空DataFrameでも書ける():
    """0 行 0 列の DataFrame でも Excel を書けること。"""
    data = to_excel_bytes({"空": pd.DataFrame()})
    workbook = load_workbook(io.BytesIO(data))
    assert "空" in workbook.sheetnames


# ---------------------------------------------------------------------------
# iCalendar
# ---------------------------------------------------------------------------


def test_to_icsの前後とCRLF(solved, slots_of, small_staff):
    """VCALENDAR で始まり CRLF で終わり、CRLF 改行であること。"""
    text = to_ics(solved, slots_of, small_staff)
    assert text.startswith("BEGIN:VCALENDAR")
    assert text.rstrip().endswith("END:VCALENDAR")
    assert "\r\n" in text
    assert text.replace("\r\n", "") .count("\n") == 0


def test_to_icsは1勤務1イベント(solved, slots_of, small_staff):
    """勤務ブロック数が ``VEVENT`` 数と一致すること。"""
    text = to_ics(solved, slots_of, small_staff)
    events = text.count("BEGIN:VEVENT")
    assert events == text.count("END:VEVENT")
    working = sum(1 for a in solved.assignments if a.state is CellState.WORK)
    assert events >= 1
    assert working >= events
    for required in ("VERSION:2.0", "PRODID:", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
                     "UID:", "DTSTAMP:", "DTSTART:", "DTEND:", "SUMMARY:", "TRANSP:OPAQUE"):
        assert required in text


def test_to_icsは75octetで折り返す(solved, slots_of, small_staff):
    """RFC 5545 の 75 octet 折り返しが守られていること。"""
    text = to_ics(solved, slots_of, small_staff)
    for line in text.split("\r\n"):
        if line.startswith(" "):
            continue
        assert len(line.encode("utf-8")) <= 75, line


def test_to_icsは制御文字をエスケープする(solved, slots_of, small_staff):
    """``;`` ``,`` ``\\`` がエスケープされていること。"""
    text = to_ics(solved, slots_of, small_staff)
    for line in text.split("\r\n"):
        if line.startswith(("SUMMARY:", "DESCRIPTION:", "X-ALT-DESC")):
            body = line.split(":", 1)[1]
            assert not re.search(r"(?<!\\)[,;]", body), line


def test_to_icsは勤務0件でも構造が壊れない(small_requirements, small_staff):
    """1 人も働かないときでも VCALENDAR は閉じること。"""
    empty = solve_shift_greedy([], [], small_requirements, standard=STANDARD)
    text = to_ics(empty, tuple(small_requirements.slots), small_staff)
    assert text.startswith("BEGIN:VCALENDAR")
    assert "END:VCALENDAR" in text
    assert "BEGIN:VEVENT" not in text


# ---------------------------------------------------------------------------
# DataFrame 群
# ---------------------------------------------------------------------------


def test_shift_to_dataframeの列(solved, slots_of, small_staff):
    """長形式シフト表の列名が UI と一致すること。"""
    frame = shift_to_dataframe(solved, slots_of, small_staff)
    assert list(frame.columns) == [
        "職員ID", "氏名", "資格", "日付", "曜日", "時間帯", "開始", "終了",
        "状態", "勤務分数", "休憩分数", "早出", "遅出",
    ]
    assert set(frame["状態"]) <= {"勤務", "休憩"}


def test_shift_to_dataframeは勤務分数が整合(solved, slots_of, small_staff):
    """勤務セルだけ勤務分数が立ち、休憩セルは休憩分数が立つこと。"""
    frame = shift_to_dataframe(solved, slots_of, small_staff)
    work = frame[frame["状態"] == "勤務"]
    rest = frame[frame["状態"] == "休憩"]
    assert (work["勤務分数"] == 30).all()
    assert (work["休憩分数"] == 0).all()
    if len(rest):
        assert (rest["休憩分数"] == 30).all()


def test_shift_to_dataframe_include_off(solved, slots_of, small_staff):
    """``include_off=True`` で全セル写出されること。"""
    on = shift_to_dataframe(solved, slots_of, small_staff)
    off = shift_to_dataframe(solved, slots_of, small_staff, include_off=True)
    assert len(off) > len(on)
    assert set(off["状態"]) <= {"勤務", "休憩", "オフ"}


def test_shift_to_dataframeは空でも列が維持される(small_requirements, small_staff):
    """0 件でも列が潰れないこと。"""
    empty = solve_shift_greedy([], [], small_requirements, standard=STANDARD)
    frame = shift_to_dataframe(empty, tuple(small_requirements.slots), small_staff)
    assert frame.empty
    assert "職員ID" in frame.columns


def test_shift_matrix_dataframeの軸(solved, slots_of, small_staff):
    """index=職員ID / columns=時間帯ラベルのマトリクスになること。"""
    frame = shift_matrix_dataframe(solved, DAY, slots_of, small_staff)
    assert list(frame.index) == [m.staff_id for m in small_staff]
    assert list(frame.columns) == [s.label for s in slots_of]
    assert frame.index.name == "職員ID"
    assert frame.columns.name == "時間帯"
    assert set(frame.to_numpy().ravel()) <= {"勤務", "休憩", "オフ"}


def test_shift_matrix_dataframeは未知の日で全オフ(solved, slots_of, small_staff):
    """存在しない日を引くと全職員が ``オフ`` になること。"""
    frame = shift_matrix_dataframe(solved, date(2030, 1, 1), slots_of, small_staff)
    assert (frame.to_numpy() == "オフ").all()


def test_shift_matricesは全日汇总(solved, slots_of, small_staff):
    """全日を 1 枚にまとめた表の列が「日付/曜日/職員ID/時間帯…」になること。"""
    frame = shift_matrices(solved, slots_of, small_staff)
    assert list(frame.columns)[:3] == ["日付", "曜日", "職員ID"]
    assert len(frame) == len(small_staff)
    assert frame.loc[0, "日付"] == DAY.isoformat()


def test_shift_matricesは0日でも列が維持される(small_requirements, small_staff):
    """0 件のときでも列が潰れないこと。"""
    empty = solve_shift_greedy([], [], small_requirements, standard=STANDARD)
    frame = shift_matrices(empty, tuple(small_requirements.slots), small_staff)
    assert frame.empty
    assert list(frame.columns)[:3] == ["日付", "曜日", "職員ID"]


def test_requirements_dataframeの列(small_requirements):
    """必要人員表の列名が UI と一致し、``表示日付`` を持つこと。"""
    frame = requirements_dataframe(small_requirements)
    assert "表示日付" in frame.columns
    assert "必要人員" in frame.columns
    assert frame.loc[0, "表示日付"] == "9/28(月)"


def test_requirements_dataframeは空でも列が崩れない():
    """行が 0 件の ``RequirementTable`` でも列が固定であること。"""
    empty = RequirementTable(DAY_OPEN, DAY_CLOSE, 30, ())
    frame = requirements_dataframe(empty)
    assert frame.empty
    assert list(frame.columns) == [
        "日付", "表示日付", "時間帯", "開始", "終了", "年齢クラス",
        "在園児数", "必要人員", "必要保育士数", "時間帯区分", "根拠", "必須",
    ]


def test_payroll_dataframeの列(solved, slots_of, small_staff, facility):
    """給与計算表が UI が必要とする列を持つこと。"""
    frame = payroll_dataframe(solved, slots_of, small_staff, facility)
    assert len(frame) == len(small_staff)
    assert {"職員ID", "氏名", "資格"} <= set(frame.columns)
    assert "推定人件費" in frame.columns
    assert (frame["実働時間"] >= 0).all()


def test_payroll_dataframeは人件費設定に効く(solved, slots_of, small_staff):
    """単価を変えると推定人件費が変わること。"""
    cheap = payroll_dataframe(solved, slots_of, small_staff, FacilitySettings(labor_cost_per_hour=1000.0))
    dear = payroll_dataframe(solved, slots_of, small_staff, FacilitySettings(labor_cost_per_hour=2000.0))
    assert dear["推定人件費"].sum() == pytest.approx(cheap["推定人件費"].sum() * 2, rel=0.01)


def test_payroll_dataframeは職員数ぶん行がある(small_requirements):
    """勤務 0 人でも全職員の行が残ること。"""
    empty = solve_shift_greedy([], [], small_requirements, standard=STANDARD)
    pool = [small_pool_member("P001", "正職", sei_contract()),
            small_pool_member("P002", "パート", part_contract())]
    frame = payroll_dataframe(empty, tuple(small_requirements.slots), pool)
    assert list(frame["職員ID"]) == ["P001", "P002"]
    assert (frame["実働時間"] == 0).all()
    assert (frame["推定人件費"] == 0).all()


def small_pool_member(staff_id, name, contract):
    from shiftai.domain import Role, StaffMember

    return StaffMember(staff_id, name, (Role.HOIKUSHI,), contract)


# ---------------------------------------------------------------------------
# サマリー
# ---------------------------------------------------------------------------


def test_summary_markdownの構造(solved, slots_of, small_staff, facility):
    """マークダウンが主要見出しと表を持つこと。"""
    text = summary_markdown(solved, slots_of, small_staff, facility_name=facility.facility_name)
    assert text.startswith("# シフトサマリー:")
    assert "あさひ保育園" in text
    assert "配置基準の不足セル" in text
    assert "法令違反（ブロッカー）" in text
    assert "## 職員別勤務時間" in text
    assert "| 職員ID | 氏名 | 資格 | 勤務日数 | 実働時間 | 休憩時間 |" in text
    for member in small_staff:
        assert member.staff_id in text


def test_summary_markdownは期間を指定できる(solved, slots_of, small_staff):
    """``period`` を渡すと対象期間が明記されること。"""
    text = summary_markdown(
        solved, slots_of, small_staff, period=(date(2026, 9, 28), date(2026, 9, 30))
    )
    assert "2026年9月28日(月)" in text
    assert "2026年9月30日(水)" in text


def test_summary_markdownは0件でも壊れない(small_requirements, small_staff):
    """勤務 0 件でも見出しと職員表が出ること。"""
    empty = solve_shift_greedy([], [], small_requirements, standard=STANDARD)
    text = summary_markdown(empty, tuple(small_requirements.slots), small_staff)
    assert "# シフトサマリー" in text
    assert "## 職員別勤務時間" in text


def test_summary_markdownはメッセージを含める(solved, slots_of, small_staff):
    """``messages`` があるときは「メッセージ」節が出ること。"""
    text = summary_markdown(solved, slots_of, small_staff)
    if solved.messages:
        assert "## メッセージ" in text


# ---------------------------------------------------------------------------
# ZIP 束
# ---------------------------------------------------------------------------


def test_export_bundle_zipの中身(solved, small_requirements, slots_of, small_staff, facility):
    """ZIP に UI が約束する 6 ファイルが揃うこと。"""
    data = export_bundle_zip(solved, small_requirements, slots_of, small_staff, facility)
    assert isinstance(data, bytes)
    assert data[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        assert names == {
            "shift.csv", "payroll.csv", "shift_matrix.csv",
            "requirements.csv", "shift.ics", "summary.md", "shift.xlsx",
        }
        assert archive.testzip() is None


def test_export_bundle_zipのCSVはBOM付き(solved, small_requirements, slots_of, small_staff, facility):
    """ZIP 内の CSV も Excel 互換の BOM 付きであること。"""
    data = export_bundle_zip(solved, small_requirements, slots_of, small_staff, facility)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in ("shift.csv", "payroll.csv", "shift_matrix.csv", "requirements.csv"):
            assert archive.read(name).startswith(b"\xef\xbb\xbf")


def test_export_bundle_zipの成果物が開ける(solved, small_requirements, slots_of, small_staff, facility):
    """Excel・CSV・ICS を実際にパースできること。"""
    data = export_bundle_zip(solved, small_requirements, slots_of, small_staff, facility)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        workbook = load_workbook(io.BytesIO(archive.read("shift.xlsx")))
        assert {"シフト", "職員別勤務", "給与計算", "配置基準"} <= set(workbook.sheetnames)
        shift = pd.read_csv(io.BytesIO(archive.read("shift.csv")), encoding="utf-8-sig")
        assert "職員ID" in shift.columns
        ics = archive.read("shift.ics").decode("utf-8")
        assert ics.startswith("BEGIN:VCALENDAR")
        assert "END:VCALENDAR" in ics
        summary = archive.read("summary.md").decode("utf-8")
        assert "# シフトサマリー" in summary


def test_export_bundle_zipはrequirements無しでも動く(solved, slots_of, small_staff, facility):
    """必要人員表を ``None`` にしても ZIP を作れること。"""
    data = export_bundle_zip(solved, None, slots_of, small_staff, facility)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        assert "requirements.csv" not in names
        assert "配置基準" not in set(load_workbook(io.BytesIO(archive.read("shift.xlsx"))).sheetnames)
