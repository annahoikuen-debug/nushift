"""Timefold の ConstraintVerifier 思想に基づく制約単体振る舞い検証フレームワーク。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from shiftai.domain import (
    CellState,
    ObjectiveWeights,
    RequirementTable,
    ShiftDay,
    ShiftScore,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    Violation,
)
from shiftai.gap_analysis import check_violations
from shiftai.score import compute_shift_score


class ConstraintVerifier:
    """宣言的制約単体検証フレームワーク。

    使用例:
        verifier = ConstraintVerifier(standard)
        verifier.verify_that("CONSECUTIVE_DAYS_EXCEEDED") \\
            .given_staff(staff_member, assignments) \\
            .triggers_violation("CONSECUTIVE_DAYS_EXCEEDED", expected_count=1)
    """

    def __init__(
        self,
        standard: StaffingStandard | None = None,
        weights: ObjectiveWeights | None = None,
    ):
        self.standard = standard
        self.weights = weights or ObjectiveWeights()

    def verify_that(self, constraint_id: str) -> _SingleConstraintAssertion:
        """検証対象の制約 ID を指定してアサーションビルダーを開始する。"""
        return _SingleConstraintAssertion(constraint_id, self.standard, self.weights)


class _SingleConstraintAssertion:
    """特定の制約に対するアサーション実行クラス。"""

    def __init__(
        self,
        constraint_id: str,
        standard: StaffingStandard | None,
        weights: ObjectiveWeights,
    ):
        self.constraint_id = constraint_id
        self.standard = standard
        self.weights = weights
        self._violations: list[Violation] = []
        self._score: ShiftScore | None = None

    def given(
        self,
        staff: Sequence[StaffMember],
        shift_days: Sequence[ShiftDay],
        requirements: RequirementTable,
        preferences: Mapping[str, StaffPreferences] | None = None,
    ) -> _SingleConstraintAssertion:
        """シフト配置データを与えて制約チェックとスコア計算を実行する。"""
        from shiftai.domain import SolveResult, SolveStatus

        dummy_result = SolveResult(
            status=SolveStatus.OPTIMAL,
            shift_days=list(shift_days),
        )

        # 違反チェックの実行
        self._violations = check_violations(
            requirements,
            dummy_result,
            staff,
            preferences or {},
            standard=self.standard,
        )

        # スコアの計算
        self._score = compute_shift_score(
            shift_days=shift_days,
            violations=self._violations,
            requirements=requirements,
            staff=staff,
            preferences=preferences,
            weights=self.weights,
        )

        return self

    def given_single_staff(
        self,
        member: StaffMember,
        days_states: Mapping[date, Mapping[str, CellState]],
        requirements: RequirementTable,
        preference: StaffPreferences | None = None,
    ) -> _SingleConstraintAssertion:
        """単一職員の勤務状態で検証するショートカット。"""
        shift_days = [
            ShiftDay(day=d, assignments={member.staff_id: dict(slot_dict)})
            for d, slot_dict in days_states.items()
        ]
        prefs = {member.staff_id: preference} if preference else {}
        return self.given([member], shift_days, requirements, prefs)

    def triggers_violation(self, expected_code: str | None = None, expected_count: int = 1) -> _SingleConstraintAssertion:
        """期待される違反コードと件数をアサートする。"""
        code = expected_code or self.constraint_id
        matched = [v for v in self._violations if v.code == code]
        assert (
            len(matched) == expected_count
        ), f"制約 {code} の違反件数が期待と異なります: 期待={expected_count}, 実際={len(matched)} (全違反: {[v.code for v in self._violations]})"
        return self

    def has_no_violations(self, constraint_code: str | None = None) -> _SingleConstraintAssertion:
        """違反が発生していないことをアサートする。"""
        code = constraint_code or self.constraint_id
        matched = [v for v in self._violations if v.code == code]
        assert (
            len(matched) == 0
        ), f"制約 {code} の違反が発生してはなりませんが、{len(matched)} 件検出されました: {[v.message for v in matched]}"
        return self

    def penalizes_by(self, expected_penalty: float, tol: float = 1e-2) -> _SingleConstraintAssertion:
        """期待されるスコア減点と一致するかアサートする。"""
        assert self._score is not None, "given() が先に呼ばれていません"
        match = self._score.matches.get(self.constraint_id)
        actual_impact = abs(match.score_impact) if match else 0.0
        assert (
            abs(actual_impact - expected_penalty) <= tol
        ), f"制約 {self.constraint_id} の減点が期待と異なります: 期待={expected_penalty}, 実際={actual_impact}"
        return self
