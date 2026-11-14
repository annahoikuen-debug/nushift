"""ConstraintVerifier による制約単体振る舞いテスト。

Timefold の ConstraintVerifier 思想に基づき、MILP 求解を通さずに
最小フィクスチャで制約単位の違反コード発火と減点スコアを検証する。
"""

from __future__ import annotations

import datetime as dt
from datetime import date, time

from shiftai import local_rules
from shiftai.domain import (
    CellState,
    Contract,
    EmploymentType,
    ObjectiveWeights,
    RequirementTable,
    Role,
    Slot,
    StaffMember,
    StaffPreferences,
    Unavailability,
)
from shiftai.testing.verifier import ConstraintVerifier

FQ_KEY = "福岡市"


def _make_dummy_table(days: list[date], slots: list[Slot]) -> RequirementTable:
    return RequirementTable(
        day_open=slots[0].start,
        day_close=slots[-1].end,
        granularity_min=30,
        slots=slots,
        rows={d: [] for d in days},
    )


def test_3分割勤務制約の単体検証():
    """1日3ブロック勤務（中抜け2回）の場合に SPLIT_SHIFT が発火し、2ブロックなら発火しないこと。"""
    standard = local_rules.get_standard(FQ_KEY)
    verifier = ConstraintVerifier(standard, weights=ObjectiveWeights(split_duty_penalty=5000.0))

    d = date(2026, 11, 2)
    slots = [
        Slot(time(9, 0), time(9, 30)),
        Slot(time(9, 30), time(10, 0)),
        Slot(time(10, 0), time(10, 30)),
        Slot(time(10, 30), time(11, 0)),
        Slot(time(11, 0), time(11, 30)),
        Slot(time(11, 30), time(12, 0)),
        Slot(time(12, 0), time(12, 30)),
    ]
    table = _make_dummy_table([d], slots)
    member = StaffMember(
        "S01",
        "山田",
        roles=(Role.HOIKUSHI,),
        contract=Contract(weekly_hours=40.0, daily_hours=8.0, employment_type=EmploymentType.SEI),
    )

    # 1. 3分割勤務: 9:00-9:30 (WORK), 9:30-10:00 (OFF), 10:00-10:30 (WORK), 10:30-11:00 (OFF), 11:00-11:30 (WORK)
    three_blocks = {
        slots[0].label: CellState.WORK,
        slots[1].label: CellState.OFF,
        slots[2].label: CellState.WORK,
        slots[3].label: CellState.OFF,
        slots[4].label: CellState.WORK,
        slots[5].label: CellState.OFF,
        slots[6].label: CellState.OFF,
    }
    verifier.verify_that("SPLIT_SHIFT") \
        .given_single_staff(member, {d: three_blocks}, table) \
        .triggers_violation("SPLIT_SHIFT", expected_count=1) \
        .penalizes_by(5000.0)

    # 2. 正常（連続1ブロック）: 9:00-10:30 WORK
    continuous = {
        slots[0].label: CellState.WORK,
        slots[1].label: CellState.WORK,
        slots[2].label: CellState.WORK,
        slots[3].label: CellState.OFF,
        slots[4].label: CellState.OFF,
        slots[5].label: CellState.OFF,
        slots[6].label: CellState.OFF,
    }
    verifier.verify_that("SPLIT_SHIFT") \
        .given_single_staff(member, {d: continuous}, table) \
        .has_no_violations()


def test_連続勤務日数上限の単体検証():
    """6連続出勤の場合に CONSECUTIVE_DAYS が発火し、5連勤なら発火しないこと。"""
    standard = local_rules.get_standard(FQ_KEY)
    verifier = ConstraintVerifier(standard)

    days = [date(2026, 11, 1) + dt.timedelta(days=i) for i in range(7)]
    slots = [Slot(time(9, 0), time(17, 0))]
    table = _make_dummy_table(days, slots)

    member = StaffMember(
        "S01",
        "山田",
        roles=(Role.HOIKUSHI,),
        contract=Contract(weekly_hours=40.0, daily_hours=8.0, max_consecutive_days=5),
    )

    # 1. 6連勤 (11/1 〜 11/6 WORK, 11/7 OFF)
    six_days = {days[i]: {slots[0].label: CellState.WORK} for i in range(6)}
    six_days[days[6]] = {slots[0].label: CellState.OFF}

    verifier.verify_that("CONSECUTIVE_DAYS") \
        .given_single_staff(member, six_days, table) \
        .triggers_violation("CONSECUTIVE_DAYS", expected_count=1)

    # 2. 5連勤 (11/1 〜 11/5 WORK, 11/6 〜 11/7 OFF)
    five_days = {days[i]: {slots[0].label: CellState.WORK} for i in range(5)}
    five_days[days[5]] = {slots[0].label: CellState.OFF}
    five_days[days[6]] = {slots[0].label: CellState.OFF}

    verifier.verify_that("CONSECUTIVE_DAYS") \
        .given_single_staff(member, five_days, table) \
        .has_no_violations()


def test_希望休違反の単体検証():
    """出勤不可日に WORK が割り当てられた場合に WORK_ON_UNAVAILABLE が発火すること。"""
    standard = local_rules.get_standard(FQ_KEY)
    verifier = ConstraintVerifier(standard, weights=ObjectiveWeights(preference_miss_penalty=5.0))

    d = date(2026, 11, 2)
    slots = [Slot(time(9, 0), time(17, 0))]
    table = _make_dummy_table([d], slots)

    member = StaffMember(
        "S01",
        "山田",
        roles=(Role.HOIKUSHI,),
        contract=Contract(weekly_hours=40.0, daily_hours=8.0),
    )
    pref = StaffPreferences(unavailable=[Unavailability(day=d, reason="法事")])

    # 不可日に出勤
    bad_work = {d: {slots[0].label: CellState.WORK}}
    verifier.verify_that("WORK_ON_UNAVAILABLE") \
        .given_single_staff(member, bad_work, table, preference=pref) \
        .triggers_violation("WORK_ON_UNAVAILABLE", expected_count=1) \
        .penalizes_by(50.0)

    # 不可日に公休 (OFF)
    good_off = {d: {slots[0].label: CellState.OFF}}
    verifier.verify_that("WORK_ON_UNAVAILABLE") \
        .given_single_staff(member, good_off, table, preference=pref) \
        .has_no_violations()
