"""案2（出力ゲート）と 案6（配置対象外職種）の回帰テスト。

現場実測で発見した欠陥:

* ``tab_export._checklist_items`` の最終項目が ``not violations`` で、
  ``KEY_VIOLATIONS`` は INFO を含む全件なので、``HOURS_IMBALANCE``（参考）が
  1 件あるだけで **全ダウンロードボタンが無効化**された。解除する手段もなかった。
* 配置対象外職種（調理員・園長）に ``MONTHLY_HOURS_SHORT: 0.00 時間`` が出ていた。

本ファイルが守る不変条件:

1. 参考（INFO）だけではダウンロードが封鎖されない
2. 未確認の要調整（WARNING）は封鎖する
3. 確認済みにした要調整は封鎖を解除する
4. 法令違反（BLOCKER）は確認済みでも封鎖する
5. ``Violation.fingerprint`` は文言に依存しない
6. 配置対象外職種に契約時間の警告が出ない（参考として理由だけ示す）
7. payroll に「配置対象」「拘束時間」列がある
"""

from __future__ import annotations

from datetime import date

import pytest

from shiftai import exporter
from shiftai.domain import (
    Contract,
    EmploymentType,
    Role,
    Slot,
    StaffMember,
    Violation,
    ViolationSeverity,
)
from shiftai.gap_analysis import VIOLATION_CODE_LABELS, check_violations
from shiftai.ui import state, tab_export

DAY = date(2026, 9, 28)


def _violation(severity: ViolationSeverity, code: str = "X", **kwargs) -> Violation:
    return Violation(severity=severity, code=code, message="テスト", **kwargs)


def _result(status=None, violations=None) -> object:
    from shiftai.domain import SolveResult, SolveStatus

    return SolveResult(
        status=status or SolveStatus.FEASIBLE,
        shift_days=[],
        violations=list(violations or []),
    )


class _FakeReport:
    total_shortfall_slots = 0


@pytest.fixture
def clean_session() -> None:
    """Streamlit のセッション状態を空にしておく。"""
    from shiftai.ui import state as state_mod

    state_mod.set(state_mod.KEY_VIOLATIONS, [])
    state_mod.set(state_mod.KEY_GAP_REPORT, _FakeReport())
    state_mod.set_acknowledged(())
    yield


# ---------------------------------------------------------------------------
# 1〜4. ゲートの深刻度別判定
# ---------------------------------------------------------------------------


def _gate_ok(violations, acknowledged=(), result_violations=None) -> bool:
    state.set(state.KEY_VIOLATIONS, violations)
    state.set(state.KEY_GAP_REPORT, _FakeReport())
    state.set_acknowledged(acknowledged)
    return all(
        item.ok for item in tab_export._checklist_items(_result(violations=result_violations))
    )


def test_参考だけならダウンロードは封鎖されない(clean_session) -> None:
    """修正前の実害の再現テスト。

    ``HOURS_IMBALANCE`` は参考（INFO）でしか出てこないが、
    修正前のゲートは ``not violations`` だったので全ダウンロードが無効になっていた。
    """
    assert _gate_ok([_violation(ViolationSeverity.INFO, "HOURS_IMBALANCE")]) is True


def test_未確認の要調整は封鎖する(clean_session) -> None:
    assert _gate_ok([_violation(ViolationSeverity.WARNING, "SPLIT_SHIFT")]) is False


def test_確認済みの要調整は封鎖を解除する(clean_session) -> None:
    v = _violation(ViolationSeverity.WARNING, "SPLIT_SHIFT")
    assert _gate_ok([v], acknowledged={v.fingerprint}) is True


def test_法令違反は確認済みでも封鎖する(clean_session) -> None:
    """BLOCKER は確認済みでは解除できない（安全側の不変条件）。

    法令違反のゲートは ``SolveResult.violations``（``blockers()``）を、
    要調整のゲートはセッションの ``KEY_VIOLATIONS`` を見ている。
    この2系統が独立していることを前提に書いている。
    """
    v = _violation(ViolationSeverity.BLOCKER, "WORK_ON_UNAVAILABLE")
    assert _gate_ok([], acknowledged={v.fingerprint}, result_violations=[v]) is False


def test_ゲートは最適化状態も見る(clean_session) -> None:
    """既存チェック（最適化状態）は変えていないこと。"""
    from shiftai.domain import SolveStatus

    state.set(state.KEY_VIOLATIONS, [])
    state.set(state.KEY_GAP_REPORT, _FakeReport())
    items = tab_export._checklist_items(_result(SolveStatus.INFEASIBLE))
    assert items[2].ok is False, "解なしは出力できない"
    assert items[3].ok is True, "状態に関係なく要調整ゲートは独立"


# ---------------------------------------------------------------------------
# 5. fingerprint
# ---------------------------------------------------------------------------


def test_fingerprintは文言に依存しない() -> None:
    """同じコード・日付・職員なら、条文を変えても指紋が変わらないこと。"""
    a = Violation(
        severity=ViolationSeverity.WARNING,
        code="C",
        message="旧文言",
        day=DAY,
        staff_id="S01",
    )
    b = Violation(
        severity=ViolationSeverity.WARNING,
        code="C",
        message="新しい文言",
        day=DAY,
        staff_id="S01",
    )
    assert a.fingerprint == b.fingerprint


def test_fingerprintは日付と職員で分かれる() -> None:
    a = Violation(
        severity=ViolationSeverity.WARNING, code="C", message="m", day=DAY, staff_id="S01"
    )
    b = Violation(
        severity=ViolationSeverity.WARNING, code="C", message="m", day=DAY, staff_id="S02"
    )
    c = Violation(
        severity=ViolationSeverity.WARNING,
        code="C",
        message="m",
        day=date(2026, 9, 29),
        staff_id="S01",
    )
    assert len({a.fingerprint, b.fingerprint, c.fingerprint}) == 3


def test_apply_acknowledgementsは元のリストを書き換えない(clean_session) -> None:
    v = _violation(ViolationSeverity.WARNING, "C")
    state.set_acknowledged({v.fingerprint})
    result = state.apply_acknowledgements([v])
    assert result[0].acknowledged is True
    assert v.acknowledged is False, "元の Violation を書き換えてはいけない"


def test_入力が変わると確認済みは破棄される(clean_session) -> None:
    """再計算すると違反一覧は作り直されるため、確認状態も消える。"""
    v = _violation(ViolationSeverity.WARNING, "C")
    state.set_acknowledged({v.fingerprint})
    state.invalidate_pipeline()
    assert state.acknowledged_fingerprints() == frozenset()


# ---------------------------------------------------------------------------
# 6. 配置対象外職種
# ---------------------------------------------------------------------------


def _staff(staff_id: str, roles: tuple[Role, ...], *, overtime_allowed=True) -> StaffMember:
    return StaffMember(
        staff_id=staff_id,
        name=staff_id,
        roles=roles,
        contract=Contract(
            weekly_hours=40.0,
            daily_hours=8.0,
            employment_type=EmploymentType.SEI,
            overtime_allowed=overtime_allowed,
        ),
    )


def _check_violations_for(staff: list[StaffMember]) -> list[Violation]:
    """1週間ぶんの必要人員表を用意して ``check_violations`` を呼ぶ。

    配置対象外職員はソルバの入口で除外されるので、``assignments`` は空のまま
    （勤務が 1 セルも無い）にして、違反検査の副作用だけを純粋に検証する。
    """
    from datetime import time, timedelta

    from shiftai.domain import (
        AgeClass,
        FacilitySettings,
        Requirement,
        RequirementTable,
        SolveResult,
        SolveStatus,
        to_time,
    )

    days = [date(2026, 9, 28) + timedelta(days=i) for i in range(7)]
    base = 9 * 60
    slots = [Slot(start=to_time(base + 30 * i), end=to_time(base + 30 * (i + 1))) for i in range(4)]
    table = RequirementTable(
        day_open=time(9, 0),
        day_close=time(11, 0),
        granularity_min=30,
        slots=slots,
        rows={
            d: [
                Requirement(
                    day=d,
                    slot=slot,
                    age_class=AgeClass.INFANT,
                    child_count=1,
                    needed_staff=1,
                    needed_qualified=1,
                    basis="テスト",
                )
                for slot in slots
            ]
            for d in days
        },
    )
    result = SolveResult(status=SolveStatus.FEASIBLE, shift_days=[], assignments=[])
    return check_violations(
        table,
        result,
        staff,
        {},
        standard=None,
        settings=FacilitySettings(day_open=time(9, 0), day_close=time(11, 0)),
    )


def test_配置対象外職種に契約時間の警告が出ない() -> None:
    staff = [
        _staff("S27", (Role.CHUUBOU,)),
        _staff("S28", (Role.ENJOGAKUIN,)),
    ]
    codes = {v.code for v in _check_violations_for(staff)}
    assert "MONTHLY_HOURS_SHORT" not in codes
    assert "MONTHLY_HOURS_EXCEEDED" not in codes
    assert "WEEKLY_HOURS_EXCEEDED" not in codes
    assert "DAILY_HOURS_EXCEEDED" not in codes


def test_配置対象外職種には理由が参考として出る() -> None:
    staff = [_staff("S27", (Role.CHUUBOU,))]
    violations = _check_violations_for(staff)
    notes = [v for v in violations if v.code == "NOT_PLACED_ROLE"]
    assert len(notes) == 1
    assert notes[0].severity is ViolationSeverity.INFO
    assert notes[0].staff_id == "S27"


def test_保育士には契約時間の警告が従来通り出る() -> None:
    """除外判断が保育士にも広がっていないこと（regression）。"""
    staff = [_staff("S01", (Role.HOIKUSHI,))]
    codes = {v.code for v in _check_violations_for(staff)}
    # 出勤が皆無なので未達警告は出る（除外されていない証拠）
    assert "NOT_PLACED_ROLE" not in codes
    assert codes & {"MONTHLY_HOURS_SHORT", "MONTHLY_HOURS_EXCEEDED"}


def test_NOT_PLACED_ROLEはラベルを持つ() -> None:
    """``VIOLATION_CODE_LABELS`` に無いコードは UI で説明できない。"""
    assert "NOT_PLACED_ROLE" in VIOLATION_CODE_LABELS


# ---------------------------------------------------------------------------
# 7. payroll の列
# ---------------------------------------------------------------------------


def test_payrollに配置対象と拘束時間がある() -> None:
    from datetime import time

    from shiftai.domain import ShiftDay

    slot = Slot(start=time(9, 0), end=time(9, 30))
    day = ShiftDay(day=DAY, assignments={})
    result = _result()
    result.shift_days = [day]

    frame = exporter.payroll_dataframe(
        result,
        [slot],
        [_staff("S27", (Role.CHUUBOU,)), _staff("S01", (Role.HOIKUSHI,))],
    )
    assert "配置対象" in frame.columns
    assert "拘束時間" in frame.columns
    cook = frame[frame["職員ID"] == "S27"].iloc[0]
    hoiku = frame[frame["職員ID"] == "S01"].iloc[0]
    assert str(cook["配置対象"]).startswith("×")
    assert str(hoiku["配置対象"]).startswith("○")


def test_拘束時間は勤務と休憩の和になる() -> None:
    from datetime import time

    from shiftai.domain import CellState, ShiftDay

    slots = [
        Slot(start=time(9, 0), end=time(9, 30)),
        Slot(start=time(9, 30), end=time(10, 0)),
    ]
    day = ShiftDay(day=DAY, assignments={})
    day.set("S01", slots[0], CellState.WORK)
    day.set("S01", slots[1], CellState.BREAK)
    result = _result()
    result.shift_days = [day]

    frame = exporter.payroll_dataframe(result, slots, [_staff("S01", (Role.HOIKUSHI,))])
    row = frame.iloc[0]
    assert row["総勤務時間"] == pytest.approx(0.5)
    assert row["総休憩時間"] == pytest.approx(0.5)
    assert row["拘束時間"] == pytest.approx(1.0)
