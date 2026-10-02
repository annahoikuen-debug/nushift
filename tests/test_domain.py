"""ドメインモデル（``shiftai.domain``）のテスト。

``domain.py`` は全モジュールが依存する「契約」なので、値の取り合いと
不正値の拒否（``ValueError``）をここに固定する。
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from shiftai.domain import (
    AgeClass,
    AgeRatio,
    CellState,
    ChildPlan,
    Contract,
    EmploymentType,
    FacilitySettings,
    Requirement,
    RequirementTable,
    Role,
    ShiftAssignment,
    ShiftDay,
    Slot,
    SlotKind,
    SolveStatus,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    Unavailability,
    Violation,
    ViolationSeverity,
    build_slots,
    daterange,
    format_jp_date,
    format_jp_date_full,
    is_workday,
    japanese_weekday,
    overlapping_slots,
    to_minutes,
    to_time,
)

pytestmark = pytest.mark.timeout(300)


# ---------------------------------------------------------------------------
# 時間帯
# ---------------------------------------------------------------------------


def test_slot_分数は30分():
    """30 分枠の ``minutes``/``hours`` が整合すること。"""
    slot = Slot(time(9, 0), time(9, 30))
    assert slot.minutes == 30
    assert slot.hours == pytest.approx(0.5)
    assert slot.start_minutes == 540
    assert slot.end_minutes == 570


def test_slot_ラベルはcolon区切りのHHMM形式():
    """``label`` は UI と GAS で使う固定表記であること。"""
    assert Slot(time(7, 0), time(7, 30)).label == "07:00-07:30"
    assert Slot(time(19, 0), time(19, 30)).label == "19:00-19:30"


def test_slot_不正値でValueError():
    """``end <= start`` の ``Slot`` は生成できないこと。"""
    with pytest.raises(ValueError):
        Slot(time(9, 0), time(9, 0))
    with pytest.raises(ValueError):
        Slot(time(10, 0), time(9, 0))


def test_slot_overlapsは境界を共有しない():
    """``overlaps`` は半開区間として扱い、境界一致は「重なり」と見なさないこと。"""
    slot = Slot(time(9, 0), time(10, 0))
    assert slot.overlaps(time(9, 0), time(9, 30)) is True
    assert slot.overlaps(time(9, 30), time(10, 0)) is True
    assert slot.overlaps(time(10, 0), time(11, 0)) is False
    assert slot.overlaps(time(8, 0), time(9, 0)) is False


def test_slot_containsは内包判定():
    """``contains`` は「完全に含まれる」ときだけ真。"""
    slot = Slot(time(9, 0), time(10, 0))
    assert slot.contains(time(9, 0), time(10, 0)) is True
    assert slot.contains(time(9, 30), time(9, 45)) is True
    assert slot.contains(time(8, 30), time(10, 0)) is False
    assert slot.contains(time(9, 30), time(10, 30)) is False


def test_slot_shiftedは前後へずらせる():
    """``shifted`` はminutesを保ったまま境界だけ動かすこと。"""
    slot = Slot(time(9, 0), time(9, 30))
    assert slot.shifted(30) == Slot(time(9, 30), time(10, 0))
    assert slot.shifted(-60) == Slot(time(8, 0), time(8, 30))
    assert slot.shifted(30).minutes == slot.minutes


def test_build_slots_既定は25枠():
    """7:15〜19:30・30分粒度は 25 枠（合計 12.5 時間）になること。"""
    slots = build_slots(time(7, 15), time(19, 30), 30)
    assert len(slots) == 25
    assert sum(s.hours for s in slots) == pytest.approx(12.5)
    assert slots[0].label == "07:00-07:30"
    assert slots[-1].label == "19:00-19:30"


def test_build_slots_境界は切り捨てと切り上げ():
    """粒度に合わない開所・閉所は外側へ丸めて在園時間を失わないこと。"""
    slots = build_slots(time(7, 15), time(19, 30), 60)
    assert slots[0].start == time(7, 0)
    assert slots[-1].end == time(20, 0)
    assert all(s.minutes == 60 for s in slots)


def test_build_slots_strictでValueError():
    """``strict=True`` は粒度非整除を許容せず、明示的に失敗すること。"""
    with pytest.raises(ValueError):
        build_slots(time(7, 15), time(19, 30), 30, strict=True)


def test_build_slots_不正な引数():
    """粒度 0 以下・閉所≤開所は ``ValueError``。"""
    with pytest.raises(ValueError):
        build_slots(time(9, 0), time(10, 0), 0)
    with pytest.raises(ValueError):
        build_slots(time(10, 0), time(9, 0), 30)


def test_overlapping_slotsは添字を返す():
    """``overlapping_slots`` は該当時間帯のインデックスを昇順で返すこと。"""
    slots = build_slots(time(9, 0), time(12, 0), 30)
    assert overlapping_slots(slots, time(9, 30), time(10, 30)) == [1, 2]
    assert overlapping_slots(slots, time(12, 0), time(13, 0)) == []


def test_to_minutesとto_timeは相互変換():
    """分↔時刻の変換が丸めなしで往復すること。"""
    assert to_minutes(time(7, 15)) == 435
    assert to_time(435) == time(7, 15)
    assert to_time(to_minutes(time(19, 30))) == time(19, 30)


# ---------------------------------------------------------------------------
# 年齢クラス
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("years", "expected"),
    [
        (0, AgeClass.INFANT),
        (1, AgeClass.AGE_1),
        (2, AgeClass.AGE_2),
        (3, AgeClass.AGE_3),
        (4, AgeClass.AGE_4),
        (5, AgeClass.AGE_5),
        (7, AgeClass.AGE_5),
    ],
)
def test_age_class_from_years(years, expected):
    """5 歳以上はすべて ``AGE_5`` に丸めること。"""
    assert AgeClass.from_years(years) is expected


def test_age_class_sort_keyは年齢順():
    """``sort_key`` が昇順に並ぶこと（表示順の土台）。"""
    ordered = sorted(AgeClass, key=lambda a: a.sort_key)
    assert ordered == [
        AgeClass.INFANT,
        AgeClass.AGE_1,
        AgeClass.AGE_2,
        AgeClass.AGE_3,
        AgeClass.AGE_4,
        AgeClass.AGE_5,
    ]


def test_age_class_yearsとsort_keyが一致():
    """``years`` は ``sort_key`` の別名で、両者は常に一致すること。"""
    for member in AgeClass:
        assert member.years == member.sort_key


def test_cell_state_は3種類():
    """1 セルは 勤務/休憩/オフ の 3 状態のみであること。"""
    assert {s.value for s in CellState} == {"勤務", "休憩", "オフ"}


# ---------------------------------------------------------------------------
# 配置基準
# ---------------------------------------------------------------------------


def _standard(**overrides) -> StaffingStandard:
    base = dict(
        name="テスト基準",
        ratios={
            AgeClass.INFANT: AgeRatio(AgeClass.INFANT, 3.0, "ceil"),
            AgeClass.AGE_1: AgeRatio(AgeClass.AGE_1, 6.0, "ceil"),
        },
    )
    base.update(overrides)
    return StaffingStandard(**base)


def test_headcount_for_切り上げ():
    """既定の ``ceil`` はratioを超えると必ず1名増えること。"""
    std = _standard()
    assert std.headcount_for(AgeClass.INFANT, 9) == 3
    assert std.headcount_for(AgeClass.INFANT, 10) == 4
    assert std.headcount_for(AgeClass.AGE_1, 6) == 1
    assert std.headcount_for(AgeClass.AGE_1, 7) == 2


def test_headcount_for_切り捨て():
    """``floor`` は定員比に満たない人数を 0 名に切り捨てること。"""
    std = _standard(ratios={AgeClass.INFANT: AgeRatio(AgeClass.INFANT, 3.0, "floor")})
    assert std.headcount_for(AgeClass.INFANT, 5) == 1
    assert std.headcount_for(AgeClass.INFANT, 2) == 0


def test_headcount_for_四捨五入():
    """``round`` は 0.5 以上を 1 名に繰り上げること。"""
    std = _standard(ratios={AgeClass.INFANT: AgeRatio(AgeClass.INFANT, 4.0, "round")})
    assert std.headcount_for(AgeClass.INFANT, 2) == 1
    assert std.headcount_for(AgeClass.INFANT, 3) == 1
    assert std.headcount_for(AgeClass.INFANT, 6) == 2


def test_headcount_for_在園児0名は0():
    """在園児が 0 人なら必要人員も 0 人であること。"""
    std = _standard()
    assert std.headcount_for(AgeClass.INFANT, 0) == 0
    assert std.headcount_for(AgeClass.INFANT, -3) == 0


def test_ratio_for_未定義はKeyError():
    """年齢クラスが ratios に無い場合は握り潰さず ``KeyError`` にすること。"""
    with pytest.raises(KeyError):
        _standard().ratio_for(AgeClass.AGE_5)


def test_slot_kind_早朝():
    """早朝保育時間帯は ``EARLY`` に分類されること。"""
    std = _standard()
    assert std.slot_kind(Slot(time(7, 0), time(7, 30))) is SlotKind.EARLY
    assert std.slot_kind(Slot(time(8, 0), time(8, 30))) is SlotKind.EARLY


def test_slot_kind_保育標準時間():
    """保育標準時間（8:30〜17:15）は ``STANDARD_TIME`` に分類されること。"""
    std = _standard()
    assert std.slot_kind(Slot(time(8, 30), time(9, 0))) is SlotKind.STANDARD_TIME
    assert std.slot_kind(Slot(time(17, 0), time(17, 15))) is SlotKind.STANDARD_TIME
    assert std.is_standard_time(Slot(time(8, 30), time(9, 0))) is True
    assert std.is_standard_time(Slot(time(7, 30), time(8, 0))) is False


def test_slot_kind_延長と延長緩和なし():
    """延長帯は緩和措置の有無で ``LATE`` と ``LATE_STRICT`` に分かれること。"""
    std = _standard(late_care_after_relax_time=time(18, 30))
    assert std.slot_kind(Slot(time(17, 30), time(18, 0))) is SlotKind.LATE
    assert std.slot_kind(Slot(time(19, 0), time(19, 30))) is SlotKind.LATE_STRICT
    always = _standard(late_care_after_relax_time=None)
    assert always.slot_kind(Slot(time(19, 0), time(19, 30))) is SlotKind.LATE


def test_standard_to_dict():
    """``to_dict`` は JSON 化できる素の型だけを含むこと。"""
    data = _standard().to_dict()
    assert data["name"] == "テスト基準"
    assert data["ratios"]["0歳児"]["children_per_staff"] == 3.0
    assert data["standard_time"] == ["08:30", "17:15"]


# ---------------------------------------------------------------------------
# 園児
# ---------------------------------------------------------------------------


def test_child_plan_在園時間():
    """在園時間（h）と分換算が登降園時刻から機械的に決まること。"""
    child = ChildPlan("C001", "テスト", date(2026, 9, 28), AgeClass.INFANT, time(9, 0), time(17, 0))
    assert child.stay_minutes == 480
    assert child.stay_hours == pytest.approx(8.0)


def test_child_plan_欠席は0分():
    """欠席日は在園時間を 0 として扱うこと。"""
    child = ChildPlan(
        "C001", "テスト", date(2026, 9, 28), AgeClass.INFANT, time(9, 0), time(17, 0), absent=True
    )
    assert child.stay_minutes == 0
    assert child.stay_hours == 0.0


def test_child_plan_降園が登園以下はValueError():
    """在園時間が不正な園児データは受け付けないこと。"""
    with pytest.raises(ValueError):
        ChildPlan("C001", "テスト", date(2026, 9, 28), AgeClass.INFANT, time(17, 0), time(9, 0))
    with pytest.raises(ValueError):
        ChildPlan("C001", "テスト", date(2026, 9, 28), AgeClass.INFANT, time(9, 0), time(9, 0))


def test_child_plan_早朝延長の実在園():
    """早朝/延長利用時だけ実登降園時刻が基準時間帯まで広がること。"""
    child = ChildPlan(
        "C001",
        "テスト",
        date(2026, 9, 28),
        AgeClass.INFANT,
        time(9, 0),
        time(16, 0),
        uses_early_care=True,
        uses_late_care=True,
    )
    window = (time(7, 15), time(19, 30))
    assert child.effective_arrive(window) == time(7, 15)
    assert child.effective_depart(window) == time(19, 30)
    plain = ChildPlan(
        "C002", "テスト2", date(2026, 9, 28), AgeClass.INFANT, time(9, 0), time(16, 0)
    )
    assert plain.effective_arrive(window) == time(9, 0)
    assert plain.effective_depart(window) == time(16, 0)


# ---------------------------------------------------------------------------
# 職員・契約
# ---------------------------------------------------------------------------


def _contract(**overrides) -> Contract:
    base = dict(weekly_hours=40.0, daily_hours=8.0)
    base.update(overrides)
    return Contract(**base)


def test_contract_不正値でValueError():
    """契約時間が 0 以下、または月間最大<最小の ``Contract`` は拒否されること。"""
    with pytest.raises(ValueError):
        _contract(daily_hours=0.0)
    with pytest.raises(ValueError):
        _contract(weekly_hours=-1.0)
    with pytest.raises(ValueError):
        _contract(min_monthly_hours=100.0, max_monthly_hours=50.0)


def test_contract_正常():
    """正常系では値がそのまま保持されること。"""
    contract = _contract()
    assert contract.weekly_hours == 40.0
    assert contract.employment_type is EmploymentType.PART
    assert contract.min_rest_hours == 11.0


def test_staff_member_資格判定():
    """``is_qualified`` は保育士資格のある職員だけを真にすること。"""
    hoiku = StaffMember("S001", "A", (Role.HOIKUSHI,), _contract())
    shien = StaffMember("S002", "B", (Role.SHIENSHIIN,), _contract())
    both = StaffMember("S003", "C", (Role.SHIENSHIIN, Role.HOIKUSHI), _contract())
    assert hoiku.is_qualified is True
    assert shien.is_qualified is False
    assert both.is_qualified is True
    assert both.primary_role is Role.SHIENSHIIN
    assert both.has_role(Role.HOIKUSHI) is True


def test_staff_member_資格が空はValueError():
    """資格が 1 つもない職員は登録できないこと。"""
    with pytest.raises(ValueError):
        StaffMember("S001", "A", (), _contract())


def test_staff_preferences_希望休判定():
    """``is_unavailable`` は時間帯の重なりで判定されること。"""
    day = date(2026, 9, 28)
    slot = Slot(time(9, 0), time(9, 30))
    prefs = StaffPreferences(
        unavailable=[Unavailability(day=day, start=time(0, 0), end=time(23, 59), reason="希望休")]
    )
    assert prefs.is_unavailable(day, slot) is True
    assert prefs.is_unavailable(day + timedelta(days=1), slot) is False
    assert day in prefs.unavailable_days()
    assert prefs.preferred_slots_for(day) == ()


# ---------------------------------------------------------------------------
# シフト表
# ---------------------------------------------------------------------------


def test_shift_day_get_set():
    """未設定セルは ``OFF`` を返し、``set`` で上書きできること。"""
    day = date(2026, 9, 28)
    slot = Slot(time(9, 0), time(9, 30))
    sd = ShiftDay(day=day)
    assert sd.get("S001", slot) is CellState.OFF
    sd.set("S001", slot, CellState.WORK)
    assert sd.get("S001", slot) is CellState.WORK
    assert sd.get("S002", slot) is CellState.OFF


def test_shift_day_workingは勤務者だけ():
    """``working`` は勤務セルが 1 つでもある職員のみを返すこと。"""
    day = date(2026, 9, 28)
    slots = build_slots(time(9, 0), time(10, 30), 30)
    sd = ShiftDay(day=day)
    sd.set("S001", slots[0], CellState.WORK)
    sd.set("S001", slots[1], CellState.BREAK)
    sd.set("S002", slots[0], CellState.BREAK)
    assert list(sd.working(slots)) == ["S001"]
    assert sd.working(slots)["S001"] == [slots[0]]
    assert set(sd.working_slots_by_staff(slots)) == {"S001"}


def test_shift_day_to_frame():
    """``to_frame`` は職員×時間帯のマトリクスを作ること。"""
    day = date(2026, 9, 28)
    slots = build_slots(time(9, 0), time(10, 0), 30)
    sd = ShiftDay(day=day)
    sd.set("S001", slots[0], CellState.WORK)
    frame = sd.to_frame(slots, ["S001", "S002"])
    assert list(frame.columns) == ["職員ID", "09:00-09:30", "09:30-10:00"]
    assert frame.loc[0, "09:00-09:30"] == "勤務"
    assert frame.loc[1, "09:00-09:30"] == "オフ"


def test_shift_assignment_判定():
    """``ShiftAssignment`` の is_work/is_break/key が状態と一致すること。"""
    day = date(2026, 9, 28)
    slot = Slot(time(9, 0), time(9, 30))
    work = ShiftAssignment("S001", day, slot, CellState.WORK)
    rest = ShiftAssignment("S001", day, slot, CellState.BREAK)
    off = ShiftAssignment("S001", day, slot, CellState.OFF)
    assert (work.is_work, work.is_break) == (True, False)
    assert (rest.is_work, rest.is_break) == (False, True)
    assert (off.is_work, off.is_break) == (False, False)
    assert work.key == ("S001", day, "09:00-09:30")


# ---------------------------------------------------------------------------
# 必要人員表
# ---------------------------------------------------------------------------


def test_requirement_table_集計():
    """``needed_staff``/``total_needed_hours`` が全行の和であること。"""
    day = date(2026, 9, 28)
    slot = Slot(time(9, 0), time(9, 30))
    rows = [
        Requirement(day, slot, AgeClass.INFANT, 3, 1, 1),
        Requirement(day, slot, AgeClass.AGE_1, 6, 1, 1),
    ]
    table = RequirementTable(time(9, 0), time(10, 0), 30, (slot,), {day: rows})
    assert table.needed_staff(day, slot) == 2
    assert table.needed_qualified(day, slot) == 2
    assert table.total_needed_hours() == pytest.approx(1.0)
    assert table.all_days() == [day]
    assert len(table.by_age_class(AgeClass.INFANT)) == 1
    assert table.for_day(day + timedelta(days=1)) == []


def test_requirement_table_to_long_dataframe():
    """long 形式 DataFrame が例外なく列名を持つこと。"""
    day = date(2026, 9, 28)
    slot = Slot(time(9, 0), time(9, 30))
    table = RequirementTable(
        time(9, 0),
        time(10, 0),
        30,
        (slot,),
        {day: [Requirement(day, slot, AgeClass.INFANT, 3, 1, 1, basis="3:1", is_binding=True)]},
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
    assert frame.loc[0, "根拠"] == "3:1"


def test_requirement_table_空でも壊れない():
    """行が 0 件でも DataFrame は列を持つこと。"""
    table = RequirementTable(time(9, 0), time(10, 0), 30, ())
    assert table.to_long_dataframe().empty
    assert table.all_requirements() == []


def test_requirement_shortfall_critical():
    """必須行かつ在園児がいるときだけ critical であること。"""
    day = date(2026, 9, 28)
    slot = Slot(time(9, 0), time(9, 30))
    assert Requirement(day, slot, AgeClass.INFANT, 1, 1, 1).is_shortfall_critical is True
    assert (
        Requirement(day, slot, AgeClass.INFANT, 1, 1, 1, is_binding=False).is_shortfall_critical
        is False
    )
    assert Requirement(day, slot, AgeClass.INFANT, 0, 0, 0).is_shortfall_critical is False


# ---------------------------------------------------------------------------
# 結果・違反
# ---------------------------------------------------------------------------


def test_violation_to_dict():
    """``to_dict`` は未設定要素を空文字で埋めること。"""
    day = date(2026, 9, 28)
    slot = Slot(time(9, 0), time(9, 30))
    with_day = Violation(ViolationSeverity.BLOCKER, "SHORTFALL_STAFF", "不足", day, slot, "S001")
    assert with_day.to_dict()["深刻度"] == "法令違反"
    assert with_day.to_dict()["時間帯"] == "09:00-09:30"
    bare = Violation(ViolationSeverity.INFO, "X", "参考")
    assert bare.to_dict()["日付"] == ""
    assert bare.to_dict()["職員ID"] == ""


def test_solve_status_is_str_enum():
    """``SolveStatus`` は UI 側でそのまま文字列化できること。"""
    assert SolveStatus.OPTIMAL.value == "最適解"
    assert str(SolveStatus.PARTIAL) == "SolveStatus.PARTIAL"


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def test_daterangeは両端を含む():
    """``daterange`` は start/end を含む日次イテレータであること。"""
    days = list(daterange(date(2026, 9, 28), date(2026, 10, 4)))
    assert len(days) == 7
    assert days[0] == date(2026, 9, 28)
    assert days[-1] == date(2026, 10, 4)
    assert list(daterange(date(2026, 9, 28), date(2026, 9, 28))) == [date(2026, 9, 28)]


def test_format_jp_date():
    """和暦表記（曜日つき）が UI と一致すること。"""
    assert format_jp_date(date(2026, 9, 28)) == "9/28(月)"
    assert format_jp_date_full(date(2026, 9, 28)) == "2026年9月28日(月)"
    assert japanese_weekday(date(2026, 10, 4)) == "日"


def test_is_workdayは休園日判定():
    """``is_workday`` は ``closed_days`` に含まれる日だけ False にすること。"""
    settings = FacilitySettings(closed_days=frozenset({date(2026, 9, 29)}))
    assert is_workday(date(2026, 9, 28), settings) is True
    assert is_workday(date(2026, 9, 29), settings) is False


# ---------------------------------------------------------------------------
# 深夜閉所（T-02）
# ---------------------------------------------------------------------------


def test_build_slotsは24時閉所を許可する():
    """深夜に閉所する園で最後の時間帯が 23:30〜24:00 になっても例外にしないこと。

    修正前は ``to_time(1440)`` が ``00:00`` へ折り返し、``end <= start`` で
    ``ValueError`` になっていた（24時閉所の園が計算できなかった）。
    """
    slots = build_slots(time(7, 0), time(23, 45), 30)
    assert len(slots) == 34
    assert slots[-1].start == time(23, 30)
    assert slots[-1].end == time(0, 0)


def test_終端00時の区間長は正しい():
    """``00:00`` 表記の終端は 1440 分として扱うこと。"""
    slot = Slot(time(23, 30), time(0, 0))
    assert slot.minutes == 30
    assert slot.end_minutes == 1440
    assert slot.start_minutes == 1410
    assert slot.hours == 0.5
    assert slot.is_midnight_end is True


def test_全時間帯の合計が開所から24時まで一致する():
    """隙間も重複もなく、合計が開所から 24 時までの長さになること。"""
    slots = build_slots(time(7, 0), time(23, 45), 30)
    assert sum(s.minutes for s in slots) == 17 * 60
    assert all(slots[i].end == slots[i + 1].start for i in range(len(slots) - 1))
    assert slots[0].start == time(7, 0)
    assert slots[-1].end_minutes == 1440


def test_深夜閉所でも時間境界の判定が正しい():
    """``overlaps`` / ``contains`` が 00:00 終端でも破綻しないこと。"""
    slot = Slot(time(23, 30), time(0, 0))
    assert slot.overlaps(time(23, 0), time(23, 59)) is True
    assert slot.overlaps(time(0, 0), time(1, 0)) is False
    assert slot.overlaps(time(23, 0), time(23, 30)) is False
    assert slot.contains(time(23, 30), time(0, 0)) is True


def test_深夜以外の時間帯は従来どおり():
    """00:00 始業など通常の一覧は变化的ないこと。"""
    assert Slot(time(0, 0), time(0, 30)).minutes == 30
    assert Slot(time(0, 0), time(0, 30)).end_minutes == 30
    assert Slot(time(0, 0), time(0, 30)).is_midnight_end is False
    labels = [s.label for s in build_slots(time(9, 0), time(10, 0), 30)]
    assert labels == ["09:00-09:30", "09:30-10:00"]


def test_長さ0と逆順の時間帯は引き続き拒否する():
    """番兵の緩和で既存のエラーが緩まないこと。"""
    with pytest.raises(ValueError):
        Slot(time(9, 0), time(9, 0))
    with pytest.raises(ValueError):
        Slot(time(14, 0), time(9, 0))
