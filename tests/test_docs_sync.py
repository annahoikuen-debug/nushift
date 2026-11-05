"""Static integrity guard tests ensuring no Project A contamination in documentation."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "MANUAL.md"
Q_AND_A = ROOT / "Q_AND_A.md"
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"
LICENSE = ROOT / "LICENSE"

# Forbidden keywords from Project A (open-hoikuict) that must not appear in NuShift documentation
FORBIDDEN_PROJECT_A_TERMS = (
    r"多要素認証",
    r"メールOTP",
    r"\bMFA\b",
    r"Web Push",
    r"身体測定",
    r"アレルギー管理",
    r"YouTube(簡易)?動画配信",
    r"全銀フォーマット",
    r"Zengin",
    r"登降園キオスク",
)


def test_no_project_a_terms_in_manual():
    """Ensure MANUAL.md contains zero Project A specific enterprise features."""
    content = MANUAL.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_PROJECT_A_TERMS:
        matches = re.findall(pattern, content, re.IGNORECASE)
        assert not matches, f"Forbidden term {pattern!r} found in MANUAL.md: {matches}"


def test_no_project_a_terms_in_qa():
    """Ensure Q_AND_A.md contains zero Project A specific enterprise features."""
    content = Q_AND_A.read_text(encoding="utf-8")
    for pattern in FORBIDDEN_PROJECT_A_TERMS:
        matches = re.findall(pattern, content, re.IGNORECASE)
        assert not matches, f"Forbidden term {pattern!r} found in Q_AND_A.md: {matches}"


def test_version_consistency_across_docs():
    """Verify that pyproject.toml version matches badges in README."""
    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    version_match = re.search(r'version\s*=\s*"([^"]+)"', pyproject_text)
    assert version_match, "version not found in pyproject.toml"
    current_version = version_match.group(1)

    readme_text = README.read_text(encoding="utf-8")
    assert f"version-v{current_version}" in readme_text or f"v{current_version}" in readme_text


def test_license_is_mit():
    """Verify that LICENSE file exists and specifies MIT License."""
    assert LICENSE.exists()
    content = LICENSE.read_text(encoding="utf-8")
    assert "MIT License" in content

