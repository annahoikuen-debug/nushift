import re
from pathlib import Path
import tomllib
import pytest

from shiftai import config

ROOT = Path(__file__).resolve().parent.parent


def test_color_palette_matches_emerald_theme():
    """NuShift の状態カラーが open-hoikuict と統一された Emerald/Slate パレットであること。"""
    assert config.COLOR_WORK == "#059669", f"Expected #059669 (Emerald-600), got {config.COLOR_WORK}"
    assert config.COLOR_BREAK == "#D97706", f"Expected #D97706 (Amber-600), got {config.COLOR_BREAK}"
    assert config.COLOR_SHORTFALL == "#E11D48", f"Expected #E11D48 (Rose-600), got {config.COLOR_SHORTFALL}"
    assert config.COLOR_OVER == "#4F46E5", f"Expected #4F46E5 (Indigo-600), got {config.COLOR_OVER}"


def test_streamlit_config_primary_color():
    """.streamlit/config.toml の primaryColor が #059669 (Emerald) に統一されていること。"""
    cfg_file = ROOT / ".streamlit" / "config.toml"
    assert cfg_file.is_file(), "config.toml not found"

    data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
    primary = data.get("theme", {}).get("primaryColor")
    assert primary == "#059669", f"Expected primaryColor #059669, got {primary}"

