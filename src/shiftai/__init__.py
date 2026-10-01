"""shiftai パッケージのルート。

バージョンは **pyproject.toml の ``[project].version`` が唯一の真実**。
ここではインストール済みディストリビューションのメタデータから読むことで、
ハードコードした ``__version__`` の二重管理をやめる。
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

#: インストールされていない環境（``src`` を直接 sys.path に入れた場合など）は
#: pyproject.toml の記載にフォールバックする。ここもハードコードせず、
#: pyproject を解析して取る。
_FALLBACK = "0.1.0"


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

__all__ = ["__version__", "domain"]
