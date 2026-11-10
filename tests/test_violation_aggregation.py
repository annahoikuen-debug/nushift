"""aggregate_violations() の単体テスト。"""

from datetime import date, time

from shiftai.domain import Slot, Violation, ViolationSeverity
from shiftai.gap_analysis import aggregate_violations


def make_v(
    code: str,
    staff_id: str | None,
    day: date | None,
    slot_label: str | None,
    severity: ViolationSeverity = ViolationSeverity.WARNING,
) -> Violation:
    slot = None
    if slot_label is not None:
        # "HH:MM-HH:MM" 形式を Slot に変換
        start_str, end_str = slot_label.split("-")
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        slot = Slot(time(sh, sm), time(eh, em))
    return Violation(
        severity=severity,
        code=code,
        message=f"{code} のテスト違反",
        day=day,
        slot=slot,
        staff_id=staff_id,
        detail={},
    )


class TestAggregateViolations:
    """Violation 集約の各種ケースを検証する。"""

    def test_3_items_same_key_merged(self) -> None:
        """同一 (code, staff_id, day) が 3 件 → 1 件に集約される。"""
        day = date(2026, 9, 28)
        vs = [
            make_v("SHORTFALL_STAFF", "S001", day, "07:15-07:45"),
            make_v("SHORTFALL_STAFF", "S001", day, "07:45-08:15"),
            make_v("SHORTFALL_STAFF", "S001", day, "08:15-08:45"),
        ]
        out = aggregate_violations(vs)
        assert len(out) == 1
        assert out[0].code == "SHORTFALL_STAFF"
        assert out[0].staff_id == "S001"
        assert out[0].day == day
        assert out[0].detail["count"] == 3
        assert out[0].detail["slots"] == ["07:15-07:45", "07:45-08:15", "08:15-08:45"]
        assert "（他 2 箇所）" in out[0].message

    def test_different_keys_not_merged(self) -> None:
        """キーが異なれば集約されない（コード違い / 職員違い / 日違い）。"""
        day1 = date(2026, 9, 28)
        day2 = date(2026, 9, 29)
        vs = [
            make_v("SHORTFALL_STAFF", "S001", day1, "07:15-07:45"),
            make_v("SHORTFALL_QUALIFIED", "S001", day1, "07:15-07:45"),  # コード違い
            make_v("SHORTFALL_STAFF", "S002", day1, "07:15-07:45"),  # 職員違い
            make_v("SHORTFALL_STAFF", "S001", day2, "07:15-07:45"),  # 日違い
        ]
        out = aggregate_violations(vs)
        assert len(out) == 4
        codes = {(v.code, v.staff_id, v.day) for v in out}
        assert codes == {
            ("SHORTFALL_STAFF", "S001", day1),
            ("SHORTFALL_QUALIFIED", "S001", day1),
            ("SHORTFALL_STAFF", "S002", day1),
            ("SHORTFALL_STAFF", "S001", day2),
        }

    def test_none_staff_id_and_none_day(self) -> None:
        """staff_id=None / day=None も同キー同士なら集約される。"""
        vs = [
            make_v("HOURS_IMBALANCE", None, None, "07:15-07:45"),
            make_v("HOURS_IMBALANCE", None, None, "07:45-08:15"),
        ]
        out = aggregate_violations(vs)
        assert len(out) == 1
        assert out[0].staff_id is None
        assert out[0].day is None
        assert out[0].detail["count"] == 2

    def test_severity_preserved(self) -> None:
        """深刻度 BLOCKER/WARNING/INFO の混在でも severity が保持される。"""
        day = date(2026, 9, 28)
        vs = [
            make_v("SHORTFALL_STAFF", "S001", day, "07:15-07:45", ViolationSeverity.BLOCKER),
            make_v("SHORTFALL_STAFF", "S001", day, "07:45-08:15", ViolationSeverity.WARNING),
            make_v("SHORTFALL_STAFF", "S001", day, "08:15-08:45", ViolationSeverity.INFO),
        ]
        out = aggregate_violations(vs)
        assert len(out) == 1
        # 最初の要素の severity を保持
        assert out[0].severity == ViolationSeverity.BLOCKER

    def test_empty_list(self) -> None:
        """空リスト → 空リスト。"""
        assert aggregate_violations([]) == []

    def test_single_item_unchanged(self) -> None:
        """1 件だけの違反はそのまま返される（集約されない）。"""
        day = date(2026, 9, 28)
        v = make_v("SHORTFALL_STAFF", "S001", day, "07:15-07:45")
        out = aggregate_violations([v])
        assert len(out) == 1
        assert out[0] is v  # 同一オブジェクト
