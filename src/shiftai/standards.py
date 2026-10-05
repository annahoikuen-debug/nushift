"""配置基準エンジン。

園児の登降園予定（:class:`~shiftai.domain.ChildPlan`）と一園の配置基準
（:class:`~shiftai.domain.StaffingStandard`）から、日付×時間帯×年齢クラスごとの
必要人員（:class:`~shiftai.domain.Requirement`）を算出する。

このモジュールの責務は「何人が必要か」を決めることだけで、
誰がどの時間帯に働くかはソルバ側の責務である。UI は
``Requirement.basis`` をそのまま説明文として表示できる。

主な設計方針:

* 短時間保育（保育標準時間のみ）園児は、保育標準時間帯の在園者数にだけ数える。
* 延長保育で代替措置が許される基準では、必要人員のうち半数を保育士として要求する。
* 保育室に在園児がいる時間帯は「2名ルール」により最低 ``min_staff_per_room`` 名まで
  底上げする。底上げは最も年少の年齢クラスの行に載せる。
* 祝日・行事日は ``is_binding=False`` にして超過配置を許容する。

算出手法の切替
--------------
``StaffingStandard.headcount_mode`` で制度ごとに算法を切り替える。

* ``per_class``（既定・認可保育所）: 年齢クラス毎に ``ceil(在園児数 / 定員比)``。
  1・2歳児・4・5歳児も別々に丸めるため、丸め誤差が積み上がる。
* ``facility_formula``（認可外保育施設）: 年齢区分ごとに小数第2位以下を切り捨て、
  **合計して 1 を加え**、小数第1位で四捨五入する。
  1・2歳児と4歳以上児は**合算**してから割る。出典は
  「企業主導型保育事業費補助金実施要綱」第3の2(4)②。

2 つの算法は結果が一致しない。実測では ``per_class`` は ``facility_formula`` より
大きい値になるため 1 名足りないと誤認し、大きい値にはならないケースが
約 0.5% あり、基準を満たしていると**誤判定**する。制度のプリセットは必ず
``facility_formula`` を指定すること。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import date, time

from shiftai import config
from shiftai.domain import (
    HEADCOUNT_FACILITY_FORMULA,
    QUALIFIED_RATIO,
    AgeClass,
    ChildPlan,
    Requirement,
    RequirementTable,
    Slot,
    SlotKind,
    StaffingStandard,
    build_slots,
    format_jp_date_full,
    to_minutes,
)

__all__ = [
    "build_requirements",
    "count_children_by_slot",
    "explain_requirement",
    "total_required_hours",
    "peak_requirement",
    "slot_kind_label",
    "ratio_label",
    "describe_window",
]

_ROUNDING_LABELS = {
    "ceil": "切り上げ",
    "floor": "切り捨て",
    "round": "四捨五入",
    "trunc1": "小数第2位以下切り捨て",
}

_MIN_TWO_TEXT = "2名ルールによる底上げ"
_HOLIDAY_TEXT = "祝日（必須ではない）"


def _hm(value: time | str) -> str:
    """時刻または文字列を ``HH:MM`` 表記に揃える。"""
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return str(value)


def describe_window(window: tuple[time, time]) -> str:
    """時間帯の組を ``HH:MM〜HH:MM`` の日本語表記にする。空区間は「設定なし」。"""
    start, end = window
    if to_minutes(start) >= to_minutes(end):
        return "設定なし"
    return f"{_hm(start)}〜{_hm(end)}"


def ratio_label(standard: StaffingStandard, age_class: AgeClass) -> str:
    """定員比を ``3:1`` 形式の日本語表記にする。"""
    return f"{standard.ratio_for(age_class).children_per_staff:g}:1"


def slot_kind_label(kind: SlotKind) -> str:
    """時間帯区分を UI 表示用の日本語にする。"""
    return {
        SlotKind.NORMAL: "通常保育",
        SlotKind.STANDARD_TIME: "保育標準時間",
        SlotKind.EARLY: "早朝保育（保育標準時間前）",
        SlotKind.LATE: "保育標準時間外（延長保育）",
        SlotKind.LATE_STRICT: "保育標準時間外（延長保育・緩和措置なし）",
    }.get(kind, kind.value)


def _is_standard_time_slot(standard: StaffingStandard, slot: Slot) -> bool:
    """短時間保育児を在園者として数えてよい時間帯か。

    ``StaffingStandard.is_standard_time`` は「時間帯が保育標準時間に完全に含まれる」
    ことを要求する。粒度が30分のときは全時間帯が正しく判定されるが、60分以上に粗い
    粒度だと保育標準時間の端が丸められて判定が外れるため、30分粒度を推奨する。
    """
    return standard.is_standard_time(slot)


def _resolve_window(child: ChildPlan, standard: StaffingStandard) -> tuple[time, time]:
    """早朝・延長保育の利用状況を反映した実在園時間帯を求める。"""
    arrive = child.effective_arrive(standard.early_care_window)
    depart = child.effective_depart(standard.late_care_window)
    return arrive, depart


def count_children_by_slot(
    children: Sequence[ChildPlan],
    day: date,
    slots: Sequence[Slot],
    standard: StaffingStandard,
) -> dict[AgeClass, list[int]]:
    """1日分の在園児数を「年齢クラス×時間帯」で数える。

    戻り値は ``{年齢クラス: [時間帯0の人数, 時間帯1の人数, ...]}``。
    キーは ``day`` に登録されている園児の年齢クラスのみ（年齢順に並べる）。

    ルール:

    * ``ChildPlan.absent`` が真の園児は 0 人。
    * ``uses_early_care`` / ``uses_late_care`` が真なら、実登園・実降園時刻を
      基準の早朝・延長保育時間帯まで広げる。
    * ``is_short_time``（短時間保育児）は保育標準時間帯にだけ数える。
      早朝保育・延長保育の在園者数には含めない。
    """
    day_children = [c for c in children if c.day == day]
    age_classes = sorted({c.age_class for c in day_children}, key=lambda a: a.sort_key)
    counts: dict[AgeClass, list[int]] = {ac: [0] * len(slots) for ac in age_classes}
    for child in day_children:
        if child.absent:
            continue
        arrive, depart = _resolve_window(child, standard)
        bucket = counts[child.age_class]
        for index, slot in enumerate(slots):
            if child.is_short_time and not _is_standard_time_slot(standard, slot):
                continue
            if slot.overlaps(arrive, depart):
                bucket[index] += 1
    return counts


def _required_qualified(standard: StaffingStandard, kind: SlotKind, needed_staff: int) -> int:
    """必要人員のうち保育士でなければならない人数。"""
    if needed_staff <= 0:
        return 0
    if standard.late_care_relaxed and kind is SlotKind.LATE:
        halved = min(needed_staff, max(1, math.ceil(needed_staff / 2)))
        floor_q = min(standard.late_care_min_qualified, needed_staff)
        return max(halved, floor_q)
    return needed_staff


def _basis_text(
    standard: StaffingStandard,
    age_class: AgeClass,
    child_count: int,
    needed_staff: int,
    needed_qualified: int,
    kind: SlotKind,
    is_binding: bool,
) -> str:
    """必要人員の根拠を日本語で組み立てる。"""
    ratio = standard.ratio_for(age_class)
    rounding = _ROUNDING_LABELS.get(ratio.rounding, ratio.rounding)
    parts = [
        f"{age_class.value} {child_count}名 ÷ {ratio.children_per_staff:g} = "
        f"{needed_staff}名（定員比{ratio.children_per_staff:g}:1、{rounding}）",
        slot_kind_label(kind),
    ]
    if standard.late_care_relaxed and kind is SlotKind.LATE and needed_qualified < needed_staff:
        parts.append(
            f"延長保育の代替措置（保育士1名＋支援員）で必要保育士数を{needed_qualified}名に緩和"
        )
    if not is_binding:
        parts.append(_HOLIDAY_TEXT)
    return "／".join(parts)


def _facility_formula_basis(
    standard: StaffingStandard,
    counts: Mapping[AgeClass, int],
    total: int,
    qual_total: int,
) -> str:
    """施設単位算出手法の内訳を ``basis`` 用の1文に組み立てる。"""
    parts: list[str] = []
    for group, n, part, ratio in standard.headcount_breakdown(counts):
        label = "・".join(ac.value for ac in group)
        parts.append(f"{label} {n}名 × 1/{ratio:g} = {part:.1f}（小数第2位以下切捨て）")
    extra = f" ＋ {standard.headcount_extra}" if standard.headcount_extra else ""
    parts.append(f"合計{extra} → 四捨五入 = {total}名（内訳は表示用で、この合計が基準値）")
    ratio_text = f"／うち保育士{qual_total}名（比率{standard.min_qualified_ratio:.0%}）"
    return "／".join(parts) + ratio_text


def _slot_requirements(
    standard: StaffingStandard,
    day: date,
    slot: Slot,
    kind: SlotKind,
    slot_counts: Mapping[AgeClass, int],
    is_binding: bool,
) -> list[Requirement]:
    """1 時間帯分の ``Requirement`` 行を作る（算出手法・資格判定により分岐）。"""
    present = [ac for ac in sorted(slot_counts, key=lambda a: a.sort_key) if slot_counts[ac] > 0]
    if not present:
        return []

    if standard.headcount_mode == HEADCOUNT_FACILITY_FORMULA:
        total = standard.headcount_for_slot(slot_counts)
        staff_alloc = standard.allocate_staff(slot_counts)
    else:
        staff_alloc = {
            age_class: standard.headcount_for(age_class, slot_counts[age_class])
            for age_class in present
        }
        total = sum(staff_alloc.values())

    if standard.qualified_mode == QUALIFIED_RATIO:
        qual_total = standard.slot_qualified_for(total)
        qual_alloc = standard.allocate_qualified(staff_alloc, qual_total)
    else:
        # 認可保育所: 行ごとに「必要人員＝必要保育士数」（延長緩和の時は半数を保育士）
        qual_total = sum(staff_alloc.values())
        qual_alloc = {
            age_class: _required_qualified(standard, kind, staff_alloc[age_class])
            for age_class in present
        }

    shared_basis = (
        _facility_formula_basis(standard, slot_counts, total, qual_total)
        if standard.headcount_mode == HEADCOUNT_FACILITY_FORMULA
        else ""
    )

    rows: list[Requirement] = []
    for age_class in present:
        needed_staff = staff_alloc[age_class]
        needed_qualified = qual_alloc.get(age_class, 0)
        if shared_basis:
            row_basis = [
                f"{age_class.value} {slot_counts[age_class]}名 → 必要人員{needed_staff}名",
                slot_kind_label(kind),
                shared_basis,
            ]
        else:
            row_basis = [
                _basis_text(
                    standard,
                    age_class,
                    slot_counts[age_class],
                    needed_staff,
                    needed_qualified,
                    kind,
                    is_binding,
                )
            ]
        if not is_binding and "祝" not in "".join(row_basis):
            row_basis.append(_HOLIDAY_TEXT)
        rows.append(
            Requirement(
                day=day,
                slot=slot,
                age_class=age_class,
                child_count=slot_counts[age_class],
                needed_staff=needed_staff,
                needed_qualified=needed_qualified,
                slot_kind=kind,
                basis="／".join(row_basis),
                is_binding=is_binding,
            )
        )
    return rows


def build_requirements(
    children: Sequence[ChildPlan],
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
    """登降園予定と配置基準から必要人員表を作る。

    Args:
        children: 全期間の園児予定（``ChildPlan.day`` で日ごとに振り分けられる）。
        days: 算出対象日。
        standard: 適用する配置基準（自治体プリセットなど）。
        day_open: 開所時刻。
        day_close: 閉所時刻。
        granularity_min: 時間帯の粒度（分）。
        closed_days: 休園日。該当日は ``rows`` に空リストを格納し notes に「休園」。
        holiday_dates: 祝日・行事日。該当日は ``is_binding=False`` にする。
        enforce_min_two: 2名ルール（保育室の最低配置人数）を適用するか。

    Returns:
        ``rows={日付: [Requirement...]}`` を持つ :class:`RequirementTable`。
        1件も ``Requirement`` がない時間帯（=在園児0人）は行を作らない。
    """
    slots = build_slots(day_open, day_close, granularity_min)
    closed = set(closed_days)
    holidays = set(holiday_dates)
    table = RequirementTable(
        day_open=day_open,
        day_close=day_close,
        granularity_min=granularity_min,
        slots=slots,
    )
    for day in days:
        if day in table.rows:
            continue
        if day in closed:
            table.rows[day] = []
            table.notes.append(f"{format_jp_date_full(day)}: 休園のため必要人員は算出しません")
            continue
        is_binding = day not in holidays
        if not is_binding:
            table.notes.append(
                f"{format_jp_date_full(day)}: 祝日（行事日）のため必須要件扱いにせず、超過配置を許容します"
            )
        counts = count_children_by_slot(children, day, slots, standard)
        if not counts:
            table.rows[day] = []
            continue
        day_rows: list[Requirement] = []
        for index, slot in enumerate(slots):
            kind = standard.slot_kind(slot)
            slot_counts = {ac: counts[ac][index] for ac in counts}
            if sum(slot_counts.values()) <= 0:
                continue
            slot_rows = _slot_requirements(standard, day, slot, kind, slot_counts, is_binding)
            slot_rows = _apply_min_two(standard, slot_rows, enforce_min_two)
            day_rows.extend(slot_rows)
        table.rows[day] = day_rows
    return table


def _apply_min_two(
    standard: StaffingStandard,
    slot_rows: list[Requirement],
    enforce_min_two: bool,
) -> list[Requirement]:
    """2名ルールによる底上げを行う。

    在園児が1名でもいる時間帯について、必要人員の合計が ``min_staff_per_room``
    に達するよう最も年少の年齢クラスの行を加算する。

    底上げ分は ``needed_qualified`` を増やさない（必要保育士数は据え置き）。
    2名のうち2人目を保育士でなくてもよい扱いになるため、
    ``docs/03_配置基準と自治体ルール.md``（3.3 の実測例）でも
    `2026年9月28日(月) 09:00-09:30／0歳児（定員比3:1）：在園1名 → 必要人員2名（うち保育士1名）／区分=保育標準時間／必須`
    と写在している。README も同じ数字（{RD_LINE}）を示例として示す。

    なお、認可保育所の職員配置基準を厳密に解釈すると2名とも保育士で
    ある必要があり、この実装はその点で弱い。基準の解釈を変える場合は
    ``docs/03``・README・本テスト（``test_2名ルール_0歳児1名でも必要人員2名``）
    を同時に更新すること。
    """
    if not enforce_min_two or not slot_rows:
        return slot_rows
    total = sum(r.needed_staff for r in slot_rows)
    if total >= standard.min_staff_per_room:
        return slot_rows
    gap = standard.min_staff_per_room - total
    target_index = min(range(len(slot_rows)), key=lambda i: slot_rows[i].age_class.sort_key)
    target = slot_rows[target_index]
    slot_rows[target_index] = replace(
        target,
        needed_staff=target.needed_staff + gap,
        needed_qualified=target.needed_qualified,
        basis=f"{target.basis}／{_MIN_TWO_TEXT}",
    )
    return slot_rows


def explain_requirement(req: Requirement, standard: StaffingStandard) -> str:
    """1行の必要人員を、UI のツールチップ表示できる1文にまとめる。"""
    try:
        ratio = ratio_label(standard, req.age_class)
    except KeyError:
        ratio = "未定義"
    binding = "必須" if req.is_binding else _HOLIDAY_TEXT
    return (
        f"{format_jp_date_full(req.day)} {req.slot.label}／{req.age_class.value}"
        f"（定員比{ratio}）：在園{req.child_count}名 → 必要人員{req.needed_staff}名"
        f"（うち保育士{req.needed_qualified}名）／区分={req.slot_kind.value}／{binding}"
    )


def total_required_hours(table: RequirementTable) -> float:
    """必要人員の総人時（=必要人員×時間帯長）を返す。"""
    return float(sum(r.needed_staff * r.slot.hours for rows in table.rows.values() for r in rows))


def peak_requirement(table: RequirementTable) -> int:
    """最も混雑した「日×時間帯」で必要な人員数の最大値を返す。"""
    peak = 0
    for rows in table.rows.values():
        by_slot: dict[Slot, int] = {}
        for req in rows:
            by_slot[req.slot] = by_slot.get(req.slot, 0) + req.needed_staff
        for value in by_slot.values():
            peak = max(peak, value)
    return peak
