"""受入検証スクリプト — サンプル7日求解後の主要違反コードの目標値を検証する。"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from shiftai.data_loader import load_children, load_preferences, load_staff, read_table
from shiftai.domain import FacilitySettings
from shiftai.gap_analysis import check_violations
from shiftai.local_rules import get_standard
from shiftai.solver import solve_shift
from shiftai.standards import build_requirements

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample"
STANDARD_KEY = "全国基準（厚労省）"


def verify_targets() -> int:
    c_df = read_table(SAMPLE_DIR / "children.csv", "children")
    s_df = read_table(SAMPLE_DIR / "staff.csv", "staff")
    p_df = read_table(SAMPLE_DIR / "preferences.csv", "preferences")

    children, _ = load_children(c_df)
    staff, _ = load_staff(s_df)
    prefs, _ = load_preferences(p_df)

    settings = FacilitySettings()
    standard = get_standard(STANDARD_KEY)
    days = sorted(list({c.day for c in children}))
    children_sub = [c for c in children if c.day in days]

    reqs = build_requirements(
        children_sub,
        days=days,
        standard=standard,
        day_open=settings.day_open,
        day_close=settings.day_close,
        closed_days=settings.closed_days,
        holiday_dates=settings.holiday_dates,
    )

    result = solve_shift(
        children=children_sub,
        staff=staff,
        requirements=reqs,
        preferences=prefs,
        settings=settings,
        standard=standard,
        time_limit_sec=30,
    )

    violations = check_violations(
        reqs,
        result,
        staff,
        preferences=prefs,
        standard=standard,
        settings=settings,
    )

    counts = Counter(f"{v.severity.value}:{v.code}" for v in violations)

    print("=== 違反コード別集計結果 ===")
    for key, count in sorted(counts.items()):
        print(f"  {key}: {count}")

    break_insufficient = counts.get("法令違反:BREAK_INSUFFICIENT", 0)
    split_shift = counts.get("要調整:SPLIT_SHIFT", 0)
    break_fragmented = counts.get("要調整:BREAK_FRAGMENTED", 0)

    print("\n=== 目標値検証 ===")
    print(f"  BREAK_INSUFFICIENT (目標: 0): {break_insufficient}")
    print(f"  SPLIT_SHIFT: {split_shift}")
    print(f"  BREAK_FRAGMENTED: {break_fragmented}")

    if break_insufficient > 0:
        print("❌ FAILED: BREAK_INSUFFICIENT が発生しています")
        return 1

    print("✅ PASSED: 法令違反 BREAK_INSUFFICIENT は 0 件です")
    return 0


if __name__ == "__main__":
    sys.exit(verify_targets())
