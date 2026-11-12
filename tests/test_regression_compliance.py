"""コンプライアンス（労働法規）の回帰防止テスト (NuShift / shiftai)。

労働基準法第32条相当（1日8時間・残業不可）と常勤換算の動作を機械的に
検証し、将来の変更による回帰を防ぐ。
"""

from __future__ import annotations

from shiftai.compliance import (
    ComplianceCheck,
    ComplianceReport,
    FacilitySpec,
    NG,
    OK,
    Regulation,
    StaffRecord,
    audit_facility,
)
from shiftai.domain import AgeClass, Role


def test_compliance_check_creation() -> None:
    """ComplianceCheck オブジェクトが作成できること。"""
    check = ComplianceCheck(
        key="TEST",
        title="テストチェック",
        status="OK",
        actual="8.0h",
        required="10.0h",
        basis="労基法第32条",
    )
    assert check.key == "TEST"


def test_compliance_report_creation() -> None:
    """ComplianceReport オブジェクトが作成できること。"""
    report = ComplianceReport(
        facility_name="テスト保育園",
        regulation=Regulation.CORPORATE_LED,
        checks=(),
    )
    assert report.facility_name == "テスト保育園"


def test_audit_facility_runs() -> None:
    """audit_facility が例外なく実行できること。"""
    spec = FacilitySpec()
    report = audit_facility(spec=spec)
    assert report is not None
    assert report.checks, "チェックが 1 件も無い"


def test_残業不可契約の8時間超過は常勤換算に反映される() -> None:
    """第32条相当: 週40h・日8h の正職員は常勤換算 1.0 人であること。

    回帰ポイント: 旧実装（週契約時間÷8）では同じ職員が 5.0 人と数わり、
    2 名で「6 名以上」の人数要件を満たしてしまう（H1 バグ）。
    """
    spec = FacilitySpec(
        regulation=Regulation.CORPORATE_LED,
        capacity=20,
        monthly_children={AgeClass.INFANT: 12},
        staff=(
            StaffRecord("H1", "A", (Role.HOIKUSHI,), 40.0),
            StaffRecord("H2", "B", (Role.HOIKUSHI,), 40.0),
        ),
        standard=None,
    )
    report = audit_facility(spec)
    headcount = next(c for c in report.checks if c.key == "headcount")
    # 12 名 ÷ 3 = 4 名必要に対し、常勤 2 名（FTE 2.0）では不足
    assert headcount.status == NG
    assert "保育士 2.00 人" in headcount.actual


def test_基準を満たす構成は適合になる() -> None:
    """常勤換算で必要数を満たす構成は OK になること。"""
    spec = FacilitySpec(
        regulation=Regulation.CORPORATE_LED,
        capacity=20,
        monthly_children={AgeClass.INFANT: 12},
        staff=tuple(
            StaffRecord(f"H{i}", f"職員{i}", (Role.HOIKUSHI,), 40.0) for i in range(1, 6)
        ),
        standard=None,
    )
    report = audit_facility(spec)
    headcount = next(c for c in report.checks if c.key == "headcount")
    assert headcount.status == OK
    assert "保育士 5.00 人" in headcount.actual
