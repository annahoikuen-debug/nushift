"""リポジトリ直下の conftest。

``pyproject.toml`` の ``pythonpath = ["src"]`` と重複するが無害。
``pyproject.toml`` がない環境でも src レイアウトの import が通るようにする。
共通フィクスチャは ``tests/conftest.py`` に置いている。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"

for _path in (str(_SRC), str(_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

#: テスト中は GAS 連携を無効化したい環境変数の名前。
GAS_ENV_NAMES: tuple[str, ...] = (
    "SHIFTAI_GAS_URL",
    "SHIFTAI_GAS_SHEET",
    "SHIFTAI_GAS_SECRET",
    "SHIFTAI_GAS_TIMEOUT",
)
