"""法定閾値・判定式の単一ソース。

ソルバと検査器（gap_analysis）が**必ずここから参照する**。
値を変えたいときはこのモジュールと tests/test_statutory_rules.py だけを直す。
"""

from __future__ import annotations

# 法定定数（ここで定義し、config.py から再エクスポートする）
STATUTORY_DAILY_WORK_HOURS = 8
STATUTORY_MAX_DAILY_WORK_HOURS = 10.0
STATUTORY_OVERTIME_MULTIPLIER = 1.25
INTERNAL_DAILY_LONG_HOURS = STATUTORY_DAILY_WORK_HOURS + 1.0
STATUTORY_WEEKLY_WORK_HOURS = 44  # 社内目安（法定ではない）
"""週あたりの上限として用いる**社内目安値**（44 時間）。
UI で「法定の週労働時間」と断定して表示してはならない。
"""
STATUTORY_MIN_REST_HOURS = 11
STATUTORY_BREAK_THRESHOLDS = ((8 * 60, 60), (6 * 60, 45))
STATUTORY_BREAK_MINUTES = 45
STATUTORY_LONG_SHIFT_MINUTES = 6 * 60
DEFAULT_BREAK_MINUTES = 60
BAND_TOLERANCE_HOURS = 0.25  # 月間バンド判定の許容差（0.1h 超過で違反にしない）
FULL_TIME_HOURS = 8.0


def required_break_minutes(work_minutes: int) -> int:
    """労働基準法第34条に基づく必要休憩分数。"""
    need = 0
    for threshold, minutes in STATUTORY_BREAK_THRESHOLDS:
        if work_minutes > threshold:
            need = max(need, minutes)
    return need


def daily_cap_minutes(contract) -> int:
    """domain.Contract から1日の上限勤務分を計算（旧 domain.daily_cap_minutes と同一式）。

    36協定なしまたは残業不可は8時間で頭打ち。
    """
    base = int(round(contract.daily_hours * 60))
    if not contract.overtime_allowed or not contract.has_36_agreement:
        return int(min(base, STATUTORY_DAILY_WORK_HOURS * 60))
    legal_cap = int(STATUTORY_MAX_DAILY_WORK_HOURS * 60)
    return int(min(base * STATUTORY_OVERTIME_MULTIPLIER, legal_cap))
