"""shiftai パッケージのルート。

バージョンは **pyproject.toml の ``[project].version`` が唯一の真実**。
ここではインストール済みディストリビューションのメタデータから読むことで、
ハードコードした ``__version__`` の二重管理をやめる。
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

#: インストールされておらず、かつ pyproject.toml も読めない環境の表示値。
#: ``0+unknown`` は setuptools/SETUPTOOLS_SCM 等の「未計算」表現に倣ったもので、
#: ここにリリース番号を書かない（版は pyproject.toml だけが持つ）。
_FALLBACK = "0+unknown"


def _resolve_version() -> str:
    try:
        return _dist_version("shiftai")
    except PackageNotFoundError:
        pass
    try:
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if pyproject.is_file():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            found = data.get("project", {}).get("version")
            if isinstance(found, str) and found:
                return found
    except (OSError, ValueError, ImportError):  # pragma: no cover - 環境依存
        pass
    return _FALLBACK


__version__ = _resolve_version()

__all__ = ["__version__", "compliance", "domain", "local_rules", "standards"]

#: ``__all__`` に挙げたサブモジュール。``import shiftai`` の時点では読み込まず、
#: 属性として参照されたときに初めて import する（PEP 562）。
#: 以前は ``__all__`` に名前だけがあり、他のコードが先に
#: ``import shiftai.compliance`` していない限り ``shiftai.compliance`` が
#: 存在せず、``from shiftai import *`` も ``AttributeError`` で落ちていた。
_LAZY_SUBMODULES = frozenset({"compliance", "domain", "local_rules", "standards"})


def __getattr__(name: str):
    if name in _LAZY_SUBMODULES:
        import importlib

        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
