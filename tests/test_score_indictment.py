"""Timefold 思想に基づくスコア説明 (Constraint Indictment) の単体テスト。"""

from __future__ import annotations

from datetime import date

from shiftai.domain import (
    ObjectiveWeights,
    ShiftScore,
    Violation,
    ViolationSeverity,
)
from shiftai.score import compute_shift_score


def test_indictment_accumulation():
    """職員×日付ごとのペナルティ起因分析が正しく集計されるか検証。"""
    d1 = date(2026, 4, 1)
    d2 = date(2026, 4, 2)

    v1 = Violation(
        code="CONSECUTIVE_DAYS",
        severity=ViolationSeverity.BLOCKER,
        message="連続勤務が上限を超えています",
        day=d1,
        staff_id="staff_A",
    )
    v2 = Violation(
        code="WORK_ON_UNAVAILABLE",
        severity=ViolationSeverity.WARNING,
        message="希望休の日に勤務が割り当てられています",
        day=d1,
        staff_id="staff_A",
    )
    v3 = Violation(
        code="BREAK_OVERLAP",
        severity=ViolationSeverity.WARNING,
        message="同時休憩が発生しています",
        day=d2,
        staff_id="staff_B",
    )
    v4 = Violation(
        code="SHORTFALL_STAFF",
        severity=ViolationSeverity.BLOCKER,
        message="必要人数が不足しています",
        day=d2,
        staff_id=None,
    )

    score = compute_shift_score(
        shift_days=[],
        violations=[v1, v2, v3, v4],
        weights=ObjectiveWeights(),
    )

    assert isinstance(score, ShiftScore)
    assert not score.is_feasible
    assert score.hard_score == -2  # v1 and v4 are BLOCKER

    # Indictments の検証
    ind_a_d1 = score.indictments.get(("staff_A", d1))
    assert ind_a_d1 is not None
    assert ind_a_d1.staff_id == "staff_A"
    assert ind_a_d1.day == d1
    assert ind_a_d1.hard_penalty_count == 1
    assert ind_a_d1.soft_penalty_total > 0
    assert len(ind_a_d1.violations) == 2
    assert ind_a_d1.total_penalty >= 1000.0

    # 施設全体の違反（staff_id is None）
    ind_fac_d2 = score.indictments.get(("__FACILITY__", d2))
    assert ind_fac_d2 is not None
    assert ind_fac_d2.hard_penalty_count == 1

    # 集計プロパティの検証
    staff_penalties = score.staff_indictments
    assert "staff_A" in staff_penalties
    assert "staff_B" in staff_penalties
    assert staff_penalties["staff_A"] > staff_penalties["staff_B"]

    day_penalties = score.day_indictments
    assert d1 in day_penalties
    assert d2 in day_penalties

    # to_dict() の検証
    score_dict = score.to_dict()
    assert "indictments" in score_dict
    assert f"staff_A_{d1}" in score_dict["indictments"]


def test_shift_score_comparison_and_medium():
    """Timefold の Hard/Medium/Soft 辞書式比較演算子の動作検証。"""
    # 1. Hard 優先
    s1 = ShiftScore(hard_score=0, medium_score=-500.0, soft_score=-1000.0)
    s2 = ShiftScore(hard_score=-1, medium_score=0.0, soft_score=0.0)
    assert s1 > s2
    assert s2 < s1

    # 2. Hard が同じなら Medium 優先
    s3 = ShiftScore(hard_score=0, medium_score=-100.0, soft_score=-500.0)
    s4 = ShiftScore(hard_score=0, medium_score=-200.0, soft_score=-50.0)
    assert s3 > s4
    assert s4 < s3

    # 3. Hard と Medium が同じなら Soft で比較
    s5 = ShiftScore(hard_score=0, medium_score=-100.0, soft_score=-50.0)
    s6 = ShiftScore(hard_score=0, medium_score=-100.0, soft_score=-80.0)
    assert s5 > s6
    assert s6 <= s5
    assert s5 >= s6

    # 4. 同値
    s7 = ShiftScore(hard_score=0, medium_score=-100.0, soft_score=-50.0)
    assert s5 == s7
    assert s5 >= s7
    assert s5 <= s7
