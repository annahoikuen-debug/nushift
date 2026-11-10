"""違反ベースラインテスト。

サンプル7日を解き、違反コード×深刻度の件数スナップショットを
tests/fixtures/violation_baseline.json に保存・検証する。
初回実行時は JSON を生成して pass し、2回目以降は既存 JSON と比較する。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from shiftai.data_loader import load_children, load_preferences, load_staff, read_table
from shiftai.domain import FacilitySettings
from shiftai.local_rules import get_standard
from shiftai.solver import solve_shift
from shiftai.standards import build_requirements

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
BASELINE_FILE = FIXTURES_DIR / "violation_baseline.json"
SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample"
STANDARD_KEY = "全国基準（厚労省）"
TIME_LIMIT_SEC = 30


def _solve_sample() -> object:
    """サンプルデータを解いて SolveResult を返す（決定論的・7日分）。"""
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

    return solve_shift(
        children=children_sub,
        requirements=reqs,
        staff=staff,
        preferences=prefs,
        settings=settings,
        standard=standard,
        time_limit_sec=TIME_LIMIT_SEC,
    )


def violation_code_counts(result) -> dict[str, int]:
    """違反を f"{severity.value}:{code}" で集約して返す。"""
    counter: Counter[str] = Counter()
    for v in result.violations:
        counter[f"{v.severity.value}:{v.code}"] += 1
    return dict(counter)


def test_violation_baseline():
    """ゴールデンマスター方式のベースラインテスト。

    JSON が無ければ生成して pass（初回）、あれば件数が一致することを検証する。
    """
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    res = _solve_sample()
    current_counts = violation_code_counts(res)

    if not BASELINE_FILE.exists():
        with open(BASELINE_FILE, "w", encoding="utf-8") as f:
            json.dump(current_counts, f, ensure_ascii=False, indent=2, sort_keys=True)
        assert len(res.assignments) > 0
        return

    with open(BASELINE_FILE, encoding="utf-8") as f:
        expected_counts = json.load(f)

    assert current_counts == expected_counts, (
        f"違反件数のベースラインが変わりました。\n"
        f"expected={expected_counts}\nactual={current_counts}"
    )
