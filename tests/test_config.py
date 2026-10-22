"""shiftai/config.py の定数をテストする。

回帰防止のため、法定定数の値を固定する。
"""

from __future__ import annotations

from shiftai import config


def test_法定定数が変わらない() -> None:
    """法定定数が変更されていないことを確認すること。"""
    # 休憩のしきい値
    assert config.STATUTORY_BREAK_THRESHOLDS == ((8 * 60, 60), (6 * 60, 45))
    # 最低休息時間
    assert config.STATUTORY_MIN_REST_HOURS == 11
    # 週の社内目安時間（厳しい方）
    assert config.STATUTORY_WEEKLY_WORK_HOURS == 44
    # 日間の法定労働時間
    assert config.STATUTORY_DAILY_WORK_HOURS == 8
    # 日間の上限時間（残業を含む）
    assert config.STATUTORY_MAX_DAILY_WORK_HOURS == 10