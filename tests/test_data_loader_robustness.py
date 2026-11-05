"""Robustness and edge-case parsing tests for data loaders."""

from __future__ import annotations

import io

import pandas as pd

from shiftai.data_loader import (
    load_children,
)


def test_data_loader_bom_and_whitespace_resilience():
    """Verify that CSV with UTF-8 BOM, extra spaces, and trailing empty lines loads cleanly."""
    csv_content = "\ufeff園児ID , 氏名 , 年齢 , 登園日 , 登園時刻 , 降園時刻 , 短時間保育 , 欠席\n"
    csv_content += " C999 , テスト 太郎 , 0 , 2026-10-02 , 08:30 , 17:30 , false , false \n\n"

    df = pd.read_csv(io.StringIO(csv_content))
    children, issues = load_children(df)

    assert len(children) == 1
    assert children[0].child_id == "C999"
    assert children[0].name == "テスト 太郎"
    assert children[0].is_short_time is False


def test_data_loader_missing_required_column_fails_gracefully():
    """Verify that missing required columns return proper issues rather than crashing unhandled."""
    df_invalid = pd.DataFrame([{"氏名": "山田 花子", "年齢": 1}])
    children, issues = load_children(df_invalid)

    # Missing mandatory columns like 園児ID, 登園日, 登園時刻, 降園時刻 must produce issues
    assert len(children) == 0
    assert len(issues) > 0

