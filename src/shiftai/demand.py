"""標準登降園需要カーブ自動生成エンジン (FacilityCapacity -> RequirementTable)。

日別の園児個別CSVが手元にない場合でも、クラスごとの定員数・認定区分比率から
厚生労働省の標準的な在園パターン（保育標準時間・短時間・早朝夕方延長）を展開し、
適法な時間帯別必要人員テーブル (RequirementTable) を直接構築する。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, time

from shiftai import config
from shiftai.domain import (
    ChildPlan,
    FacilityCapacity,
    RequirementTable,
    StaffingStandard,
)
from shiftai.standards import build_requirements


def generate_standard_children(
    capacity: FacilityCapacity,
    days: Sequence[date],
    *,
    day_open: time = time(7, 0),
    day_close: time = time(20, 0),
    standard_start: time = time(7, 30),
    standard_end: time = time(18, 30),
    short_start: time = time(8, 30),
    short_end: time = time(16, 30),
    early_care_ratio: float = 0.15,
    late_care_ratio: float = 0.20,
) -> list[ChildPlan]:
    """定員構成モデルから、標準需要パターンに基づく仮想 ChildPlan のリストを生成する。"""
    children: list[ChildPlan] = []
    child_seq = 1

    for c_cap in capacity.classes:
        age_class = c_cap.age_class

        # 1. 保育標準時間児の生成
        for i in range(c_cap.standard_time_count):
            cid = f"C{child_seq:04d}"
            name = f"{c_cap.class_name}_{i+1}"
            child_seq += 1

            # 一部園児は早朝保育・延長保育を利用
            uses_early = (i % 10) < int(early_care_ratio * 10)
            uses_late = (i % 10) < int(late_care_ratio * 10)

            arr = day_open if uses_early else standard_start
            dep = day_close if uses_late else standard_end

            for day in days:
                children.append(
                    ChildPlan(
                        child_id=cid,
                        name=name,
                        day=day,
                        age_class=age_class,
                        arrive=arr,
                        depart=dep,
                        is_short_time=False,
                        absent=False,
                        absent_reason="",
                        uses_early_care=uses_early,
                        uses_late_care=uses_late,
                    )
                )

        # 2. 保育短時間児の生成
        for i in range(c_cap.short_time_count):
            cid = f"C{child_seq:04d}"
            name = f"{c_cap.class_name}_短_{i+1}"
            child_seq += 1

            for day in days:
                children.append(
                    ChildPlan(
                        child_id=cid,
                        name=name,
                        day=day,
                        age_class=age_class,
                        arrive=short_start,
                        depart=short_end,
                        is_short_time=True,
                        absent=False,
                        absent_reason="",
                        uses_early_care=False,
                        uses_late_care=False,
                    )
                )

    return children


def build_requirements_from_capacity(
    capacity: FacilityCapacity,
    days: Sequence[date],
    standard: StaffingStandard,
    *,
    day_open: time,
    day_close: time,
    granularity_min: int = config.DEFAULT_GRANULARITY_MIN,
    closed_days: Iterable[date] = (),
    holiday_dates: Iterable[date] = (),
    enforce_min_two: bool = True,
) -> RequirementTable:
    """定員構成から自動生成した仮想園児群に基づき、RequirementTable を構築する。"""
    children = generate_standard_children(
        capacity,
        days,
        day_open=day_open,
        day_close=day_close,
        standard_start=standard.standard_time[0],
        standard_end=standard.standard_time[1],
    )
    return build_requirements(
        children,
        days=days,
        standard=standard,
        day_open=day_open,
        day_close=day_close,
        granularity_min=granularity_min,
        closed_days=closed_days,
        holiday_dates=holiday_dates,
        enforce_min_two=enforce_min_two,
    )

