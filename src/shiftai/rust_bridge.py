"""Rust 計算・検証コア（nushift_core）の透過ブリッジ。

コンパイル済み `nushift_core`（PyO3）が存在する場合は Rust 実装を呼び出し、
未ビルド環境では `statutory_rules.py` による純粋 Python 実装へ安全にフォールバックする。
"""

from __future__ import annotations

import json
from typing import Any

from shiftai import statutory_rules

try:
    import nushift_core as _rust_core  # type: ignore[import-not-found]

    RUST_CORE_AVAILABLE = True
except ImportError:
    _rust_core = None
    RUST_CORE_AVAILABLE = False


def required_break_minutes(work_minutes: int) -> int:
    if _rust_core is not None:
        return int(_rust_core.required_break_minutes(int(work_minutes)))
    return statutory_rules.required_break_minutes(work_minutes)


def validate_staff_day(payload: dict[str, Any]) -> dict[str, Any]:
    if _rust_core is not None:
        return json.loads(_rust_core.validate_staff_day_json(json.dumps(payload)))

    work_minutes = int(payload.get("work_minutes", 0))
    break_minutes = int(payload.get("break_minutes", 0))
    daily_hours = float(payload.get("daily_hours", 8.0))
    overtime_allowed = bool(payload.get("overtime_allowed", False))
    has_36_agreement = bool(payload.get("has_36_agreement", False))
    consecutive = int(payload.get("consecutive_work_days", 0))
    max_consecutive = int(payload.get("max_consecutive_days", 0))
    staff_id = str(payload.get("staff_id", ""))

    issues: list[dict[str, str]] = []
    req_break = statutory_rules.required_break_minutes(work_minutes)
    if work_minutes > 0 and break_minutes < req_break:
        issues.append(
            {
                "level": "error",
                "code": "STATUTORY_BREAK_SHORTAGE",
                "message": (
                    f"実働{work_minutes}分に対して休憩{break_minutes}分が不足しています"
                    f"（法定必要休憩: {req_break}分）。"
                ),
                "staff_id": staff_id,
            }
        )

    base = int(round(daily_hours * 60))
    if not overtime_allowed or not has_36_agreement:
        cap = min(base, statutory_rules.STATUTORY_DAILY_WORK_HOURS * 60)
    else:
        legal_cap = int(statutory_rules.STATUTORY_MAX_DAILY_WORK_HOURS * 60)
        cap = min(int(round(base * statutory_rules.STATUTORY_OVERTIME_MULTIPLIER)), legal_cap)

    if work_minutes > cap:
        issues.append(
            {
                "level": "error",
                "code": "DAILY_CAP_EXCEEDED",
                "message": f"1日の勤務上限（{cap}分）を{work_minutes - cap}分超過しています。",
                "staff_id": staff_id,
            }
        )

    if max_consecutive > 0 and consecutive > max_consecutive:
        issues.append(
            {
                "level": "warning",
                "code": "MAX_CONSECUTIVE_DAYS",
                "message": f"連続勤務{consecutive}日が上限（{max_consecutive}日）を超えています。",
                "staff_id": staff_id,
            }
        )

    return {
        "ok": not any(i["level"] == "error" for i in issues),
        "issues": issues,
    }
