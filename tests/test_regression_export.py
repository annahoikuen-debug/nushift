"""エクスポートの回帰防止テスト (NuShift / shiftai)。

Excel / CSV / iCal 出力の動作を機械的に検証し、将来の変更による
回帰を防ぐ。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from shiftai.domain import Contract, Role, Slot, StaffMember
from shiftai.exporter import to_csv_bytes, to_excel_bytes, to_ics
from shiftai.solver import SolveResult, SolveStatus


def test_to_csv_bytes_produces_output() -> None:
    """to_csv_bytes がバイト列を生成すること。"""
    df = pd.DataFrame([["職員", "日付", "シフト"], ["S001", "10/02", "早番"]])
    result = to_csv_bytes(df)
    assert isinstance(result, (bytes, bytearray))
    assert len(result) > 0


def test_to_excel_bytes_produces_output() -> None:
    """to_excel_bytes がバイト列を生成すること。"""
    df = pd.DataFrame([["職員", "日付", "シフト"], ["S001", "10/02", "早番"]])
    sheets = {"シフト": df}
    result = to_excel_bytes(sheets)
    assert isinstance(result, (bytes, bytearray))
    assert len(result) > 0


def test_to_ics_produces_output() -> None:
    """to_ics が iCal 文字列を生成すること。"""
    slots = [Slot(dt.time(9, 0), dt.time(9, 30))]
    contract = Contract(weekly_hours=40.0, daily_hours=8.0)
    staff = [
        StaffMember("S001", "職員1", (Role.HOIKUSHI,), contract),
    ]
    result = SolveResult(
        status=SolveStatus.OPTIMAL,
        shift_days=[],
        assignments=[],
        violations=[],
        messages=[],
        stats={},
    )
    ics = to_ics(result, slots, staff)
    assert isinstance(ics, str)
    assert len(ics) > 0
