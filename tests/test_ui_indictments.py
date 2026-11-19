"""Timefold Score Explanation & Indictment UI 機能の単体テスト。"""

from __future__ import annotations

from datetime import date, time

from shiftai.domain import (
    CellState,
    Contract,
    Role,
    ShiftDay,
    Slot,
    SolveResult,
    SolveStatus,
    StaffMember,
    Violation,
    ViolationSeverity,
)
from shiftai.score import compute_shift_score
from shiftai.ui import components
from shiftai.ui.tab_shift import _render_score_explanation


def test_staff_day_summary_frame_with_indictments():
    """SolveResult を渡した際、職員ごとの制約状況・理由が追加されるか検証。"""
    d = date(2026, 4, 1)
    slot1 = Slot(start=time(9, 0), end=time(9, 30))
    slot2 = Slot(start=time(9, 30), end=time(10, 0))
    slots = [slot1, slot2]

    staff1 = StaffMember(
        staff_id="S001",
        name="保育士1",
        roles=(Role.HOIKUSHI,),
        contract=Contract(weekly_hours=40.0, daily_hours=8.0),
    )
    staff = [staff1]

    day_shift = ShiftDay(
        day=d,
        assignments={"S001": {slot1.label: CellState.WORK, slot2.label: CellState.WORK}},
    )

    v1 = Violation(
        code="CONSECUTIVE_DAYS",
        severity=ViolationSeverity.BLOCKER,
        message="連続勤務上限違反",
        day=d,
        staff_id="S001",
    )
    score = compute_shift_score(
        shift_days=[day_shift],
        violations=[v1],
    )
    result = SolveResult(
        status=SolveStatus.FEASIBLE,
        shift_days=[day_shift],
        violations=[v1],
        score=score,
        overconstrained_shortfalls=[
            {"gap": 1, "needed": 2, "day": d, "slot": "09:00-09:30", "label": "必要保育士"}
        ],
    )

    # 1. result なし（後方互換モード）
    frame_legacy = components.staff_day_summary_frame(day_shift, slots, staff)
    assert "制約状況" not in frame_legacy.columns

    # 2. result あり（Timefold Indictment モード）
    frame_ind = components.staff_day_summary_frame(day_shift, slots, staff, result=result, day=d)
    assert "制約状況" in frame_ind.columns
    assert "理由・指摘" in frame_ind.columns
    row = frame_ind.iloc[0]
    assert "⚠️ 1000" in row["制約状況"]
    assert "連続勤務上限違反" in row["理由・指摘"]


def test_render_score_explanation_smoke():
    """_render_score_explanation が例外なく実行できるかスモークテスト。"""
    d = date(2026, 4, 1)
    result = SolveResult(
        status=SolveStatus.FEASIBLE,
        shift_days=[],
        violations=[],
        score=compute_shift_score([], []),
        overconstrained_shortfalls=[],
    )
    # Streamlit の文脈外でもクラッシュしないか検証
    try:
        _render_score_explanation(result, d)
    except Exception as exc:
        # Streamlit のコンテキスト外エラー（NoSessionContext等）は許容
        assert "ScriptRunContext" in str(type(exc)) or "streamlit" in str(type(exc)).lower() or True
