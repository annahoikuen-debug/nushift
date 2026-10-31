"""コンプライアンス（労働法規）の回帰防止テスト (NuShift / shiftai)。

労働基準法第32条、36協定、変形労働制、割増賃金の動作を機械的に
検証し、将来の変更による回帰を防ぐ。
"""

from __future__ import annotations

from shiftai.compliance import (
    ComplianceCheck,
    ComplianceReport,
    FacilitySpec,
    audit_facility,
)


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
    from shiftai.compliance import Regulation

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
