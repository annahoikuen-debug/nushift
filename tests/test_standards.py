"""配置基準エンジン（``shiftai.standards``）のテスト。

定員比→必要人員、2名ルール、延長保育の代替措置、保育標準時間・短時間保育の扱い、
休園日・祝日の扱い、そして「根拠」を埋め込んだ日本語テキストが UI で開示できることを保証する。
"""

from __future__ import annotations

from datetime import date, time

import pytest

from shiftai.domain import (
    AgeClass,
    AgeRatio,
    ChildPlan,
    Requirement,
    RequirementTable,
    Slot,
    SlotKind,
    StaffingStandard,
    build_slots,
)
from shiftai.standards import (
    build_requirements,
    count_children_by_slot,
    describe_window,
    explain_requirement,
    peak_requirement,
    ratio_label,
    slot_kind_label,
    total_required_hours,
)

pytestmark = pytest.mark.timeout(300)

DAY = date(2026, 9, 28)
OPEN, CLOSE = time(9, 0), time(14, 0)


def _kids(*specs, day: date = DAY) -> list[ChildPlan]:
    """(child_id, age_class, arrive, depart, kwargs) から ChildPlan を作る。"""
    out: list[ChildPlan] = []
    for index, spec in enumerate(specs):
        child_id, age_class, arrive, depart = spec[:4]
        extra = spec[4] if len(spec) > 4 else {}
        out.append(
            ChildPlan(
                f"C{index + 1:03d}", f"園児{index + 1}", day, age_class, arrive, depart, **extra
            )
        )
    return out


def _build(children, standard, *, days=(DAY,), closed=(), holidays=(), enforce_min_two=True):
    return build_requirements(
        children,
        list(days),
        standard,
        day_open=OPEN,
        day_close=CLOSE,
        granularity_min=30,
        closed_days=closed,
        holiday_dates=holidays,
        enforce_min_two=enforce_min_two,
    )


# ---------------------------------------------------------------------------
# 定員比 → 必要人員
# ---------------------------------------------------------------------------


def test_定員比_0歳児9名は3名(fq_standard):
    """0歳児 9 名 ÷ 3:1 = 3 名。切り上げが効いていること。"""
    assert fq_standard.headcount_for(AgeClass.INFANT, 9) == 3


def test_定員比_1歳児6名は2名(fq_standard):
    """福岡市は 1 歳児 4:1 なので 6 名 → 2 名（切り上げ）になること。"""
    assert fq_standard.headcount_for(AgeClass.AGE_1, 6) == 2
    assert fq_standard.ratio_for(AgeClass.AGE_1).children_per_staff == pytest.approx(4.0)


def test_定員比_全国基準は1歳児6名で1名(standard):
    """全国基準（6:1）では 6 名 → 1 名。自治体ごとに違うことの確認。"""
    assert standard.headcount_for(AgeClass.AGE_1, 6) == 1
    assert standard.ratio_for(AgeClass.INFANT).children_per_staff == pytest.approx(3.0)


def test_定員比_3歳児8名4歳児20名(standard):
    """全国基準の 3 歳児 8:1 / 4・5 歳児 20:1 であること。"""
    assert standard.ratio_for(AgeClass.AGE_3).children_per_staff == pytest.approx(8.0)
    assert standard.ratio_for(AgeClass.AGE_4).children_per_staff == pytest.approx(20.0)
    assert standard.ratio_for(AgeClass.AGE_5).children_per_staff == pytest.approx(20.0)


def test_build_requirements_行数が在園時間帯に一致(fq_standard):
    """必要人員表が「在園児がいる時間帯 × 年齢クラス」の行だけを持つこと。"""
    kids = _kids(
        ("C1", AgeClass.INFANT, time(9, 0), time(14, 0)),
        ("C2", AgeClass.AGE_1, time(9, 0), time(14, 0)),
    )
    table = _build(kids, fq_standard)
    assert len(table.for_day(DAY)) == 2 * len(table.slots)


def test_在園児0名の時間帯は行を作らない(fq_standard):
    """登降園時間帯の外側は行を作らない（柔軟な運用）。"""
    kids = _kids(("C1", AgeClass.INFANT, time(10, 0), time(11, 0)))
    table = _build(kids, fq_standard)
    labels = {r.slot.label for r in table.for_day(DAY)}
    assert labels == {"10:00-10:30", "10:30-11:00"}


# ---------------------------------------------------------------------------
# 2名ルール
# ---------------------------------------------------------------------------


def test_2名ルール_0歳児1名でも必要人員2名(fq_standard):
    """在園児が 1 名でも保育室には 2 名以上を配置すること（needed_qualified は 1）。"""
    kids = _kids(("C1", AgeClass.INFANT, time(9, 0), time(11, 0)))
    rows = _build(kids, fq_standard).for_day(DAY)
    assert rows
    assert {r.needed_staff for r in rows} == {2}
    assert {r.needed_qualified for r in rows} == {1}
    assert all("2名ルール" in r.basis for r in rows)


def test_2名ルール_offで必要人員1名(fq_standard):
    """``enforce_min_two=False`` で底上げしないこと（園ごとの上乗せ解除）。"""
    kids = _kids(("C1", AgeClass.INFANT, time(9, 0), time(11, 0)))
    rows = _build(kids, fq_standard, enforce_min_two=False).for_day(DAY)
    assert {r.needed_staff for r in rows} == {1}
    assert all("2名ルール" not in r.basis for r in rows)


def test_2名ルール_合計2名以上なら底上げしない(fq_standard):
    """定員比だけで 2 名に達するなら底上げしないこと。"""
    kids = _kids(
        ("C1", AgeClass.INFANT, time(9, 0), time(11, 0)),
        ("C2", AgeClass.AGE_1, time(9, 0), time(11, 0)),
    )
    rows = _build(kids, fq_standard).for_day(DAY)
    assert {r.needed_staff for r in rows} == {1}
    per_slot: dict[str, int] = {}
    for row in rows:
        per_slot[row.slot.label] = per_slot.get(row.slot.label, 0) + row.needed_staff
    assert set(per_slot.values()) == {2}
    assert all("2名ルール" not in r.basis for r in rows)


# ---------------------------------------------------------------------------
# 不変条件
# ---------------------------------------------------------------------------


def test_必要保育士数は必要人員以下(week_days, fq_standard):
    """``needed_qualified <= needed_staff`` が全行で成り立つこと。"""
    from shiftai import sample_data

    children, _, _ = sample_data.make_dataset(week_days, seed=42)
    table = build_requirements(
        children, week_days, fq_standard, day_open=time(7, 15), day_close=time(19, 30)
    )
    for row in table.all_requirements():
        assert row.needed_qualified <= row.needed_staff
        assert row.needed_staff >= 0


def test_必要人員は正(week_days, fq_standard):
    """行が立つなら必要人員は必ず 1 以上であること。"""
    from shiftai import sample_data

    children, _, _ = sample_data.make_dataset(week_days, seed=42)
    table = build_requirements(
        children, week_days, fq_standard, day_open=time(7, 15), day_close=time(19, 30)
    )
    assert all(r.needed_staff >= 1 for r in table.all_requirements())


# ---------------------------------------------------------------------------
# 延長保育・保育標準時間・短時間保育
# ---------------------------------------------------------------------------


def test_延長緩和_福岡市は保育士が少于必要人員(fq_standard):
    """福岡市は延長帯で「保育士1名＋支援員」の代替措置が使え、必要保育士数が少なくなること。"""
    assert fq_standard.late_care_relaxed is True
    kids = _kids(*[(f"C{i}", AgeClass.INFANT, time(17, 30), time(18, 30)) for i in range(4)])
    table = build_requirements(
        kids,
        [DAY],
        fq_standard,
        day_open=time(17, 15),
        day_close=time(18, 30),
        enforce_min_two=False,
    )
    rows = table.for_day(DAY)
    assert rows
    assert all(r.slot_kind is SlotKind.LATE for r in rows)
    assert all(r.needed_qualified < r.needed_staff for r in rows)
    assert all(r.needed_qualified <= r.needed_staff for r in rows)
    assert any("代替措置" in r.basis for r in rows)


def test_延長緩和_offでは必要保育士数と必要人員が等しい(standard):
    """``late_care_relaxed=False`` の基準では全時間帯で保育士が全和水となること。"""
    assert standard.late_care_relaxed is False
    kids = _kids(*[(f"C{i}", AgeClass.INFANT, time(17, 30), time(19, 0)) for i in range(4)])
    table = build_requirements(
        kids,
        [DAY],
        standard,
        day_open=time(17, 15),
        day_close=time(19, 30),
        enforce_min_two=False,
    )
    assert table.for_day(DAY)
    assert all(r.needed_qualified == r.needed_staff for r in table.for_day(DAY))


def test_延長緩和なしは常に必要保育士数と必要人員が等しい(fq_standard):
    """``LATE_STRICT``（緩和期限以降）は代替措置が使えず全和水になること。"""
    kids = _kids(*[(f"C{i}", AgeClass.INFANT, time(19, 0), time(19, 30)) for i in range(4)])
    table = build_requirements(
        kids,
        [DAY],
        fq_standard,
        day_open=time(19, 0),
        day_close=time(19, 30),
        enforce_min_two=False,
    )
    rows = table.for_day(DAY)
    assert rows
    assert all(r.slot_kind is SlotKind.LATE_STRICT for r in rows)
    assert all(r.needed_qualified == r.needed_staff for r in rows)


def test_短時間保育児は保育標準時間帯にだけ在園(fq_standard):
    """短時間保育児（保育標準時間のみ）は延長・早朝時間帯の在園者数に数えないこと。"""
    kids = _kids(
        ("C1", AgeClass.INFANT, time(8, 30), time(17, 15), {"is_short_time": True}),
    )
    slots = build_requirements(
        kids, [DAY], fq_standard, day_open=time(7, 15), day_close=time(19, 30)
    ).slots
    counts = count_children_by_slot(kids, DAY, tuple(slots), fq_standard)
    row = counts[AgeClass.INFANT]
    outside = [
        slots[i].label
        for i, n in enumerate(row)
        if n > 0 and not fq_standard.is_standard_time(slots[i])
    ]
    assert outside == []
    assert sum(row) > 0


def test_短時間保育児は延長帯で必要人員0(fq_standard):
    """延長時間帯で短時間保育児しか居ない場合、必要人員表に延長帯の行が出ないこと。"""
    kids = _kids(
        ("C1", AgeClass.INFANT, time(8, 30), time(17, 15), {"is_short_time": True}),
    )
    table = build_requirements(
        kids, [DAY], fq_standard, day_open=time(7, 15), day_close=time(19, 30)
    )
    assert table.for_day(DAY)
    assert all(fq_standard.is_standard_time(r.slot) for r in table.for_day(DAY))


# ---------------------------------------------------------------------------
# 休園日・祝日
# ---------------------------------------------------------------------------


def test_休園日は行が空(fq_standard, week_days):
    """休園日は ``rows[day] == []`` になり、notes に理由が入ること。"""
    closed = week_days[2]
    kids = _kids(
        *[(f"C{i}", AgeClass.INFANT, time(9, 0), time(11, 0)) for i in range(3)],
        day=closed,
    )
    table = _build(kids, fq_standard, days=week_days, closed={closed})
    assert table.rows[closed] == []
    assert any("休園" in note for note in table.notes)


def test_祝日は必須要件でない(fq_standard, week_days):
    """祝日・行事日は全行 ``is_binding=False`` になること。"""
    holiday = week_days[3]
    kids = _kids(
        *[(f"C{i}", AgeClass.INFANT, time(9, 0), time(11, 0)) for i in range(3)],
        day=holiday,
    )
    table = _build(kids, fq_standard, days=week_days, holidays={holiday})
    rows = table.for_day(holiday)
    assert rows
    assert {r.is_binding for r in rows} == {False}
    assert all("祝日" in r.basis for r in rows)
    assert any("祝日" in note for note in table.notes)


def test_通常日は必須要件(fq_standard):
    """休園日でも祝日でもない日は ``is_binding=True`` であること。"""
    kids = _kids(("C1", AgeClass.INFANT, time(9, 0), time(11, 0)))
    assert {r.is_binding for r in _build(kids, fq_standard).for_day(DAY)} == {True}


# ---------------------------------------------------------------------------
# 根拠テキスト・集計
# ---------------------------------------------------------------------------


def test_根拠に日本語の定員比が入っている(week_days, fq_standard):
    """``basis`` に定員比と丸め方（切り上げ等）の日本語が入っていること。"""
    from shiftai import sample_data

    children, _, _ = sample_data.make_dataset(week_days, seed=42)
    table = build_requirements(
        children, week_days, fq_standard, day_open=time(7, 15), day_close=time(19, 30)
    )
    for row in table.all_requirements():
        assert "定員比" in row.basis
        assert "1" in row.basis
        assert any(word in row.basis for word in ("切り上げ", "切り捨て", "四捨五入"))


def test_総必要人時とピーク(week_days, fq_standard):
    """1 週間・福岡市サンプルで 521.0 人時・ピーク 9 名であること。"""
    from shiftai import sample_data

    children, _, _ = sample_data.make_dataset(week_days, seed=42)
    table = build_requirements(
        children, week_days, fq_standard, day_open=time(7, 15), day_close=time(19, 30)
    )
    assert total_required_hours(table) == pytest.approx(521.0)
    assert peak_requirement(table) == 9
    assert table.total_needed_hours() == pytest.approx(521.0)


def test_to_long_dataframeが例外なく列を持つ(week_days, fq_standard):
    """long 形式 DataFrame の列名が契約通りであること。"""
    from shiftai import sample_data

    children, _, _ = sample_data.make_dataset(week_days, seed=42)
    table = build_requirements(
        children, week_days, fq_standard, day_open=time(7, 15), day_close=time(19, 30)
    )
    frame = table.to_long_dataframe()
    assert list(frame.columns) == [
        "日付",
        "時間帯",
        "開始",
        "終了",
        "年齢クラス",
        "在園児数",
        "必要人員",
        "必要保育士数",
        "時間帯区分",
        "根拠",
        "必須",
    ]
    assert len(frame) == len(table.all_requirements())
    assert frame["日付"].is_monotonic_increasing


def test_count_children_by_slotが例外を投げない(week_days, fq_standard):
    """在園者数カウントがこけず、年齢クラス×時間帯の配列になること。"""
    from shiftai import sample_data

    children, _, _ = sample_data.make_dataset(week_days, seed=42)
    slots = build_slots(time(7, 15), time(19, 30), 30)
    counts = count_children_by_slot(children, week_days[0], slots, fq_standard)
    assert set(counts) == {
        AgeClass.INFANT,
        AgeClass.AGE_1,
        AgeClass.AGE_2,
        AgeClass.AGE_3,
        AgeClass.AGE_4,
        AgeClass.AGE_5,
    }
    assert all(len(v) == len(slots) for v in counts.values())


def test_explain_requirementが例外を投げない(fq_standard):
    """1 行の説明文（UI のツールチップ）が生成できること。"""
    kids = _kids(("C1", AgeClass.INFANT, time(9, 0), time(11, 0)))
    row = _build(kids, fq_standard).for_day(DAY)[0]
    text = explain_requirement(row, fq_standard)
    assert "2026年9月28日" in text
    assert "必要人員" in text
    assert "定員比" in text


def test_explain_requirementは未定義年齢クラスを握り潰さない(fq_standard):
    """定員比が無い年齢クラスでも ``未定義`` と出して例外にしないこと。"""
    day = DAY
    slot = Slot(OPEN, time(9, 30))
    partial = StaffingStandard(
        name="部分定義",
        ratios={AgeClass.INFANT: AgeRatio(AgeClass.INFANT, 3.0, "ceil")},
    )
    row = Requirement(day, slot, AgeClass.AGE_5, 1, 1, 1)
    assert "未定義" in explain_requirement(row, partial)


def test_空の基準比率でValueError():
    """ratios が空だと ratio_for が KeyError になり握り潰さないこと。"""
    empty = StaffingStandard(name="空", ratios={})
    with pytest.raises(KeyError):
        empty.headcount_for(AgeClass.INFANT, 3)


# ---------------------------------------------------------------------------
# 表示用ヘルパー
# ---------------------------------------------------------------------------


def test_ratio_label():
    """定員比が ``3:1`` 形式の日本語表記になること。"""
    std = StaffingStandard(
        name="t", ratios={AgeClass.INFANT: AgeRatio(AgeClass.INFANT, 3.0, "ceil")}
    )
    assert ratio_label(std, AgeClass.INFANT) == "3:1"


def test_describe_window():
    """時間帯が ``HH:MM〜HH:MM`` になり、空区間は「設定なし」になること。"""
    assert describe_window((time(8, 30), time(17, 15))) == "08:30〜17:15"
    assert describe_window((time(8, 30), time(8, 30))) == "設定なし"


def test_slot_kind_label():
    """時間帯区分の表示名がすべて存在すること。"""
    for kind in SlotKind:
        assert slot_kind_label(kind)


def test_RequirementTableの参照系が未登録日で空(fq_standard):
    """存在しない日を引いても空が返り、例外にならないこと。"""
    table = RequirementTable(OPEN, CLOSE, 30, ())
    assert table.for_day(DAY) == []
    assert table.needed_staff(DAY, Slot(OPEN, time(9, 30))) == 0
    assert table.all_days() == []
    assert table.day_slots() == ()
