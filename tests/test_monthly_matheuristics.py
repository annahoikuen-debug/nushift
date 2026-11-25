"""NuShift Timefold 拡張機能: マセヒューリスティクス (Stage 2 ⇄ LNS 協調) のテスト。"""

from datetime import date, time
from unittest.mock import patch

import pytest

from shiftai import local_rules
from shiftai.domain import (
    AgeClass,
    ChildPlan,
    Contract,
    EmploymentType,
    FacilitySettings,
    Role,
    ShiftDay,
    ShiftScore,
    SolveResult,
    SolveStatus,
    StaffMember,
)
from shiftai.monthly import MonthlySolveContext, solve_monthly_hierarchical
from shiftai.shift_patterns import ShiftPattern
from shiftai.standards import build_requirements

pytestmark = pytest.mark.slow


def _make_simple_context(days_count: int = 2) -> MonthlySolveContext:
    days = [date(2026, 11, d) for d in range(1, days_count + 1)]
    staff = [
        StaffMember(
            staff_id="s1",
            name="職員1",
            roles=(Role.HOIKUSHI,),
            contract=Contract(
                weekly_hours=40.0,
                daily_hours=8.0,
                employment_type=EmploymentType.SEI,
                max_consecutive_days=5,
            ),
        ),
        StaffMember(
            staff_id="s2",
            name="職員2",
            roles=(Role.HOIKUSHI,),
            contract=Contract(
                weekly_hours=20.0,
                daily_hours=4.0,
                employment_type=EmploymentType.PART,
                max_consecutive_days=4,
            ),
        ),
    ]
    contracts = {s.staff_id: s.contract for s in staff}
    children = [
        ChildPlan(
            child_id=f"c_{i}",
            name=f"園児_{i}",
            day=d,
            age_class=AgeClass.AGE_1,
            arrive=time(8, 30),
            depart=time(16, 30),
        )
        for d in days
        for i in range(2)
    ]
    patterns = [
        ShiftPattern(key="NORMAL", label="日勤", start=time(8, 30), end=time(17, 30)),
        ShiftPattern(key="PART", label="短時間", start=time(8, 30), end=time(12, 30)),
    ]
    settings = FacilitySettings()
    standard = local_rules.get_standard(local_rules.DEFAULT_PRESET_KEY)
    reqs = build_requirements(
        children=children,
        days=days,
        standard=standard,
        day_open=settings.day_open,
        day_close=settings.day_close,
    )
    return MonthlySolveContext(
        target_month="2026-11",
        all_days=days,
        staff=staff,
        contracts=contracts,
        preferences={},
        patterns=patterns,
        requirements=reqs,
        facility_settings=settings,
        fixed_assignments={},
        prior_duty_history={},
        monthly_holiday_target=0,
        children=children,
        standard=standard,
    )


def test_monthly_matheuristics_disabled_by_default():
    ctx = _make_simple_context(days_count=2)
    events = []

    with patch("shiftai.local_search.ruin_and_recreate") as mock_rar:
        res = solve_monthly_hierarchical(
            ctx,
            time_limit_sec=5.0,
            progress_callback=lambda p: events.append(p["stage"]),
            enable_matheuristics=False,
        )
        assert mock_rar.call_count == 0
        assert "STAGE_3_MATHEURISTICS" not in events
        assert res.stats.get("matheuristics_improved") is None


def test_monthly_matheuristics_triggered_when_enabled():
    ctx = _make_simple_context(days_count=2)
    events = []

    fake_improved_score = ShiftScore(hard_score=0, medium_score=0.0, soft_score=-5.0)
    fake_repaired_res = SolveResult(
        status=SolveStatus.OPTIMAL,
        shift_days=[ShiftDay(day=d) for d in ctx.all_days],
        violations=[],
        score=fake_improved_score,
        stats={"local_search_iterations": 10},
    )

    with patch("shiftai.score.compute_shift_score") as mock_score:
        mock_score.return_value = ShiftScore(hard_score=-10, medium_score=0.0, soft_score=-20.0)

        with patch(
            "shiftai.local_search.ruin_and_recreate", return_value=fake_repaired_res
        ) as mock_rar:
            res = solve_monthly_hierarchical(
                ctx,
                time_limit_sec=5.0,
                progress_callback=lambda p: events.append(p["stage"]),
                enable_matheuristics=True,
            )

            assert mock_rar.call_count == 1
            assert "STAGE_3_MATHEURISTICS" in events
            assert res.stats.get("matheuristics_improved") is True
            assert res.stats.get("local_search_iterations") == 10
            assert res.score == fake_improved_score


def test_monthly_matheuristics_error_handled_gracefully():
    ctx = _make_simple_context(days_count=2)

    with patch("shiftai.score.compute_shift_score") as mock_score:
        mock_score.return_value = ShiftScore(hard_score=-5, medium_score=0.0, soft_score=-10.0)

        with patch(
            "shiftai.local_search.ruin_and_recreate", side_effect=RuntimeError("LNS failure")
        ):
            res = solve_monthly_hierarchical(
                ctx,
                time_limit_sec=5.0,
                enable_matheuristics=True,
            )
            assert "LNS failure" in res.stats.get("matheuristics_error", "")
