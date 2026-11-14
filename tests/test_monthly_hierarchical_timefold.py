"""NuShift Timefold 拡張機能（進捗イベント・協調的キャンセル・分割並列探索）のテスト。"""

import datetime as dt
from datetime import date, time

from shiftai import local_rules
from shiftai.domain import (
    AgeClass,
    CancellationToken,
    ChildPlan,
    Contract,
    EmploymentType,
    FacilitySettings,
    Role,
    SolveStatus,
    StaffMember,
)
from shiftai.monthly import MonthlySolveContext, solve_monthly_hierarchical
from shiftai.shift_patterns import ShiftPattern
from shiftai.standards import build_requirements


def _make_context(days_count: int = 3) -> MonthlySolveContext:
    days = [date(2026, 11, d) for d in range(1, days_count + 1)]

    staff = []
    contracts = {}
    for i in range(1, 3):
        sid = f"staff_{i:02d}"
        c = Contract(
            weekly_hours=40.0,
            daily_hours=8.0,
            employment_type=EmploymentType.SEI,
            max_consecutive_days=5,
        )
        s = StaffMember(
            staff_id=sid,
            name=f"常勤保育士_{i}",
            roles=(Role.HOIKUSHI,),
            contract=c,
        )
        staff.append(s)
        contracts[sid] = c

    for i in range(3, 5):
        sid = f"staff_{i:02d}"
        c = Contract(
            weekly_hours=20.0,
            daily_hours=4.0,
            employment_type=EmploymentType.PART,
            max_consecutive_days=4,
        )
        s = StaffMember(
            staff_id=sid,
            name=f"パート保育士_{i}",
            roles=(Role.HOIKUSHI,),
            contract=c,
        )
        staff.append(s)
        contracts[sid] = c

    children = []
    for d in days:
        for cid in range(1, 6):
            children.append(
                ChildPlan(
                    child_id=f"c_{cid:03d}",
                    name=f"園児_{cid}",
                    day=d,
                    age_class=AgeClass.AGE_1,
                    arrive=time(8, 30),
                    depart=time(16, 30),
                )
            )

    patterns = [
        ShiftPattern(key="NORMAL", label="日勤", start=time(8, 30), end=time(17, 30)),
        ShiftPattern(key="HALF", label="半日", start=time(8, 30), end=time(12, 30)),
    ]

    settings = FacilitySettings()
    standard = local_rules.get_standard(local_rules.DEFAULT_PRESET_KEY)
    req_table = build_requirements(
        children=children,
        days=days,
        standard=standard,
        day_open=time(8, 0),
        day_close=time(18, 0),
    )

    return MonthlySolveContext(
        target_month="2026-11",
        all_days=days,
        staff=staff,
        contracts=contracts,
        preferences={},
        patterns=patterns,
        requirements=req_table,
        facility_settings=settings,
        fixed_assignments={},
        prior_duty_history={},
        monthly_holiday_target=1,
        children=children,
        standard=standard,
    )


def test_progress_callback_reports_stages():
    """求解フェーズの進捗がコールバックへ正しく通知されるか検証。"""
    ctx = _make_context(days_count=7)
    progress_records = []

    def on_progress(p: dict):
        progress_records.append(p)

    res = solve_monthly_hierarchical(
        context=ctx,
        time_limit_sec=20,
        progress_callback=on_progress,
        parallel_workers=1,
    )

    assert res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    assert len(progress_records) >= 3
    stages = [p["stage"] for p in progress_records]
    assert "STAGE_1_PATTERNS" in stages
    assert "STAGE_1_DONE" in stages
    assert "DONE" in stages
    last = progress_records[-1]
    assert last["progress_percent"] == 100
    assert "hard_score" in last


def test_parallel_workers_partitioned_search():
    """Stage 2 の Partitioned Search (並列ワーカー) が正常に動作するか検証。"""
    ctx = _make_context(days_count=7)
    progress_records = []

    res = solve_monthly_hierarchical(
        context=ctx,
        time_limit_sec=30,
        parallel_workers=2,
        progress_callback=lambda p: progress_records.append(p),
    )

    assert res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    assert len(res.shift_days) == 7
    assert res.stats["parallel_workers"] == 2


def test_cancellation_token_stops_and_returns_checkpoint():
    """途中で協調的キャンセルされた際、即座に中断し暫定解が返却されるか検証。"""
    ctx = _make_context(days_count=7)
    token = CancellationToken()

    def cancel_on_stage2(p: dict):
        if p.get("stage") == "STAGE_2_SLOTS":
            token.cancel()

    res = solve_monthly_hierarchical(
        context=ctx,
        time_limit_sec=30,
        cancellation_token=token,
        progress_callback=cancel_on_stage2,
        parallel_workers=1,
    )

    assert res.stats.get("cancelled") is True
    assert res.status == SolveStatus.PARTIAL
    assert len(res.shift_days) < 7
