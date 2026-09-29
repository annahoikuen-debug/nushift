"""Streamlit UI 層。

バックエンド（``domain`` / ``standards`` / ``local_rules`` / ``solver`` /
``gap_analysis`` / ``data_loader`` / ``sample_data`` / ``exporter`` / ``gas_client``）
の薄い表示層のみを担当する。
"""

from __future__ import annotations

__all__ = [
    "components",
    "sidebar",
    "state",
    "tab_data",
    "tab_export",
    "tab_requirements",
    "tab_shift",
    "tab_solve",
    "theme",
]
