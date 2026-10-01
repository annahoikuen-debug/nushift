"""subprocess の出力をロケール非依存に読むための共通ヘルパ。

**なぜ必要か**
``subprocess.run(..., text=True)`` はデコードにロケールの既定エンコーディング
を使う。Windows の日本語環境では cp932、西洋 ロケールでは cp1252 になる。
本プロジェクトは日本語を大量に出力し、CLI は（``__main__._force_utf8_streams``
により）**常に UTF-8** で書くため、親側で cp932 / cp1252 でデコードすると::

    UnicodeDecodeError: 'charmap' codec can't decode byte 0x81 ...

で落ちる。しかもこれは ``subprocess`` の内部リーダースレッドで起きるため、
``stdout`` が ``None`` になり、本来の失敗原因が隠れる
（``AttributeError: 'NoneType' object has no attribute 'splitlines'``）。
**テストが環境依存で落ちたように見える**ため、原因の特定が極めて難しい。

**方針**
子プロセスには ``PYTHONIOENCODING=utf-8`` を明示し、親側でも ``encoding="utf-8"``
でデコードする。ロケールに依存しない挙動になる。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

#: 子プロセスに日本語出力を UTF-8 でさせるための環境変数の名前。
IO_ENCODING_ENV = "PYTHONIOENCODING"


def utf8_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """``PYTHONIOENCODING=utf-8`` を差し込んだ環境変数のコピーを返す。"""
    env = dict(os.environ if base is None else base)
    env[IO_ENCODING_ENV] = "utf-8"
    return env


def run_utf8(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """子プロセスを実行し、stdout / stderr を **必ず UTF-8 として**読む。

    デコードに失敗しても例外にしない（``errors="replace"``）。
    原因を後で追えるよう、文字列を返す。
    """
    return subprocess.run(
        list(args),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=utf8_env(env),
        cwd=str(cwd) if cwd is not None else None,
        timeout=timeout,
        check=False,
    )


def run_module_utf8(
    module: str,
    *args: str,
    cwd: Path | str | None = None,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """``python -m <module> <args...>`` を UTF-8 で実行する。"""
    return run_utf8(
        [sys.executable, "-m", module, *args], cwd=cwd, timeout=timeout, env=env
    )


__all__ = ["IO_ENCODING_ENV", "run_module_utf8", "run_utf8", "utf8_env"]
