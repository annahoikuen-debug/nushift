"""職員間の「早番・遅番・土曜出勤」の公平性集計と、均等度の評価。

**このモジュールが解決する問題**

``solver`` が罰している ``hours_imbalance_penalty`` は **勤務時間（時間数）** の偏りである。
一日 8 時間の两端に 1 時間ずつずらした同じ時間数でも、

    A: 早番 5 日 ／ 遅番 0 日 ／ 土曜 0 日
    B: 早番 0 日 ／ 遅番 5 日 ／ 土曜 4 日

のようなシフトは、時間数では完全に均衡でも現場では「明显に不公平」である。
主眼はここにある。

区分の定義（いずれも **勤務ブロック** を単位とする。時間帯単位ではない）

* ``early``: 勤務ブロックの開始時刻が ``standard.early_care_window[0]`` 以下。
  勤務パターンが有効なら ``early`` 枠との一致を優先して判定する。
* ``late``: 勤務ブロックの終了時刻が ``standard.late_care_window[1]`` 以降。
* ``saturday``: ``weekday() == 5`` に出勤した日数。

日曜は休園日のため原則 0 であり、平日を超えていたら休園日設定と矛盾するので対象にしない。

集計は **週単位**（:func:`shiftai.domain.weekly_periods`）で行う。
全期間を 1 つの袋で均すと「前半 2 週だけ偏り、後半で相殺される」状態を隠してしまうため。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from shiftai import config
from shiftai.domain import (
    CellState,
    Slot,
    SolveResult,
    StaffingStandard,
    StaffMember,
    to_minutes,
    weekly_periods,
)
from shiftai.shift_patterns import ShiftPattern

__all__ = [
    "CATEGORIES",
    "FairnessCounts",
    "SpreadStats",
    "block_indices",
    "category_label",
    "count_frame",
    "counts",
    "report_frame",
    "spread_stats",
]

#: 公平性を判定する区分のキー（報告の列順）。日曜は休園日のため除外する。
CATEGORIES: tuple[str, ...] = ("early", "late", "saturday")

_CATEGORY_LABELS: dict[str, str] = {
    "early": "早番",
    "late": "遅番",
    "saturday": "土曜出勤",
}


def category_label(key: str) -> str:
    """区分のキーを日本語の見出しにする（未知のキーはそのまま返す）。"""
    return _CATEGORY_LABELS.get(str(key), str(key))


def _pattern_window(patterns: Sequence[ShiftPattern], key: str) -> tuple[int, int] | None:
    """``early`` / ``late`` パターンの ``(開始分, 終了分)`` を返す。"""
    for pattern in patterns or ():
        if pattern.key == key:
            return pattern.start_minutes, pattern.end_minutes
    return None


def block_indices(states: Sequence[CellState]) -> list[tuple[int, int]]:
    """1 日の在勤（勤務+休憩）を連続ブロックごとに ``(開始添字, 終了添字)`` で返す。

    ``exporter._work_blocks`` と同じ定義だが、こちらは **在勤**（休憩を含む）で
    ブロックを切る。早番・遅番は「いつ働いたか」で判定するため、
    昼休みで分けた W-B-W も 2 ブロックとして数える。
    """
    blocks: list[tuple[int, int]] = []
    for i, state in enumerate(states):
        if state is CellState.OFF:
            continue
        if blocks and i == blocks[-1][1] + 1:
            blocks[-1] = (blocks[-1][0], i)
        else:
            blocks.append((i, i))
    return blocks


@dataclass(frozen=True)
class FairnessCounts:
    """職員 1 名について「期間全体」と「週ごと」の区分回数を持つ。"""

    staff_id: str
    totals: dict[str, int]
    weekly: dict[str, list[int]]
    """区分 → 週ごとの回数（週の並びは :func:`shiftai.domain.weekly_periods` と同じ）。"""


@dataclass(frozen=True)
class SpreadStats:
    """ある区分の「最大−最小」レンジの要約。"""

    category: str
    max_spread: int
    """週レンジの最大値。"""
    mean_spread: float
    """週レンジの平均。"""
    max_count: int
    """期間全体でもっとも多く割り当てられた回数。"""
    min_count: int
    """期間全体でもっとも少なく割り当てられた回数。"""

    @property
    def label(self) -> str:
        return category_label(self.category)


def _early_boundary(standard: StaffingStandard) -> int:
    return to_minutes(standard.early_care_window[0])


def _late_boundary(standard: StaffingStandard) -> int:
    return to_minutes(standard.late_care_window[1])


def _is_early_block(
    slots: Sequence[Slot],
    start: int,
    end: int,
    standard: StaffingStandard | None,
    early_window: tuple[int, int] | None,
) -> bool:
    """勤務ブロックが「早番」か。

    勤務パターンが指定されていれば、その枠に収まるかを先に確かめる。
    一致しなければ、基準の早朝保育時間帯で判定する。
    """
    if early_window is not None:
        window_start, window_end = early_window
        if slots[start].start_minutes >= window_start and slots[end].end_minutes <= window_end:
            return True
    if standard is None:
        return False
    return slots[start].start_minutes <= _early_boundary(standard)


def _is_late_block(
    slots: Sequence[Slot],
    start: int,
    end: int,
    standard: StaffingStandard | None,
    late_window: tuple[int, int] | None,
) -> bool:
    """勤務ブロックが「遅番」か（:func:`_is_early_block` の対称）。"""
    if late_window is not None:
        window_start, window_end = late_window
        if slots[start].start_minutes >= window_start and slots[end].end_minutes <= window_end:
            return True
    if standard is None:
        return False
    return slots[end].end_minutes >= _late_boundary(standard)


def _period_index(periods: Sequence[Sequence[date]], day: date) -> int | None:
    """その日がどの週に属するかを返す（該当なしなら ``None``）。"""
    for index, window in enumerate(periods):
        if day in window:
            return index
    return None


def counts(
    result: SolveResult,
    staff: Sequence[StaffMember],
    slots: Sequence[Slot],
    *,
    standard: StaffingStandard | None = None,
    patterns: Sequence[ShiftPattern] = (),
    days: Sequence[date] | None = None,
) -> dict[str, FairnessCounts]:
    """職員ごとの早番・遅番・土曜出勤回数を数える。

    :param result: 最適化結果
    :param staff: 職員一覧（配置対象のみでもよい）
    :param slots: 対象時間帯
    :param standard: 早朝・延長の境界を判定する基準
    :param patterns: 勤務パターン（指定すると枠一致を優先）
    :param days: 集計対象日（省略時はシフトに含まれる日）
    :returns: ``職員ID -> FairnessCounts``
    """
    early_window = _pattern_window(patterns, "early")
    late_window = _pattern_window(patterns, "late")
    shift_days = {sd.day: sd for sd in result.shift_days}
    if days is None:
        days = sorted(shift_days)
    periods = weekly_periods(list(days)) if days else []
    out: dict[str, FairnessCounts] = {}
    # 配置対象外の職員（園長・主任など ``is_placeable`` が False）は数えない。
    # ソルバの公平性目的関数も配置対象職員のみを対象にしている
    # （``_fairness_spread`` の ``placeable`` フィルタ）ため、
    # ここで含むと「最小値」が構造的に 0 に固定され、
    # UI が永远不会解消しない偏りを表示することになる。
    for member in (m for m in staff if m.is_placeable):
        sid = member.staff_id
        totals = {key: 0 for key in CATEGORIES}
        weekly = {key: [0] * len(periods) for key in CATEGORIES}
        for day in days:
            shift_day = shift_days.get(day)
            if shift_day is None:
                continue
            states = [shift_day.get(sid, slot) for slot in slots]
            if not any(state is CellState.WORK for state in states):
                continue
            index = _period_index(periods, day)
            for start, end in block_indices(states):
                if _is_early_block(slots, start, end, standard, early_window):
                    totals["early"] += 1
                    if index is not None:
                        weekly["early"][index] += 1
                if _is_late_block(slots, start, end, standard, late_window):
                    totals["late"] += 1
                    if index is not None:
                        weekly["late"][index] += 1
            if day.weekday() == config.WEEKEND_START_WEEKDAY:
                totals["saturday"] += 1
                if index is not None:
                    weekly["saturday"][index] += 1
        out[sid] = FairnessCounts(staff_id=sid, totals=totals, weekly=weekly)
    return out


def count_frame(
    result: SolveResult,
    staff: Sequence[StaffMember],
    slots: Sequence[Slot],
    *,
    standard: StaffingStandard | None = None,
    patterns: Sequence[ShiftPattern] = (),
    days: Sequence[date] | None = None,
) -> pd.DataFrame:
    """職員ごとの区分回数を「職員ID / 氏名 / 早番 / 遅番 / 土曜出勤」の表にする。"""
    tally = counts(result, staff, slots, standard=standard, patterns=patterns, days=days)
    columns = ["職員ID", "氏名", *[category_label(k) for k in CATEGORIES]]
    records = []
    for member in staff:
        entry = tally.get(member.staff_id)
        if entry is None:
            continue
        row: dict[str, Any] = {"職員ID": member.staff_id, "氏名": member.name}
        for key in CATEGORIES:
            row[category_label(key)] = entry.totals.get(key, 0)
        records.append(row)
    return pd.DataFrame.from_records(records, columns=columns)


def spread_stats(tally: dict[str, FairnessCounts], category: str) -> SpreadStats:
    """ある区分の「職員間レンジ」（週ごとの最大−最小）の要約を返す。

    :func:`spread_stats` は **職員間** の偏りを測る。つまり週 ``w`` について

        spread_w = max_s count_{s,w,k} - min_s count_{s,w,k}

    を計算し、その最大値・平均値を返す。これがソルバが最小化している量そのものなので、
    「偏りを減らせたか」を確認する数値として使う。

    職員が 1 名以下のときはレンジが常に 0 になる（定義上、公平なので）。
    """
    entries = list(tally.values())
    if not entries:
        return SpreadStats(
            category=category, max_spread=0, mean_spread=0.0, max_count=0, min_count=0
        )
    weeks = max((len(e.weekly.get(category, [])) for e in entries), default=0)
    spreads: list[int] = []
    for w in range(weeks):
        values = [
            e.weekly.get(category, [])[w] for e in entries if w < len(e.weekly.get(category, []))
        ]
        spreads.append(max(values) - min(values) if values else 0)
    all_counts = [e.totals.get(category, 0) for e in entries]
    return SpreadStats(
        category=category,
        max_spread=max(spreads) if spreads else 0,
        mean_spread=round(sum(spreads) / len(spreads), 2) if spreads else 0.0,
        max_count=max(all_counts),
        min_count=min(all_counts),
    )


def report_frame(
    result: SolveResult,
    staff: Sequence[StaffMember],
    slots: Sequence[Slot],
    *,
    standard: StaffingStandard | None = None,
    patterns: Sequence[ShiftPattern] = (),
    days: Sequence[date] | None = None,
) -> pd.DataFrame:
    """公平性のレポート表（職員ごとの回数と、その職員の週内変動）を返す。

    「◯の週内変動」は **その職員について** 週ごとの最大回数と最小回数の差。
    大きい職員ほど「回数がばらついている」ことを示す。色付けは UI 側の責務。
    職員間レンジは :func:`spread_stats` が別途返す。
    """
    tally = counts(result, staff, slots, standard=standard, patterns=patterns, days=days)
    columns = ["職員ID", "氏名"]
    for key in CATEGORIES:
        columns.append(category_label(key))
        columns.append(f"{category_label(key)}の週内変動")
    records = []
    for member in staff:
        entry = tally.get(member.staff_id)
        if entry is None:
            continue
        row: dict[str, Any] = {"職員ID": member.staff_id, "氏名": member.name}
        for key in CATEGORIES:
            row[category_label(key)] = entry.totals.get(key, 0)
            values = entry.weekly.get(key, [])
            row[f"{category_label(key)}の週内変動"] = max(values) - min(values) if values else 0
        records.append(row)
    return pd.DataFrame.from_records(records, columns=columns)
