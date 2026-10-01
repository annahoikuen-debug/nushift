"""プラットフォーム非依存性の回帰テスト（T-16）。

**このファイルが守るもの**

Windows / 日本語ロケール / UTF-8 以外のコンソールという、
「日本語環境でしか通らない」状態を検出する。いずれも実際にこのリポジトリで
発生した不具合であり、**すべて環境庆幸なしで再検出できる**。

==================================== ========================================
ID                                    実害
==================================== ========================================
T-16-R1  CLI が cp1252 で traceback  ロケールが非日本語だと全コマンドが死ぬ
T-16-R2  CLI の出力が UTF-8          出力を読む側がロケールに依存する
T-16-R3  バージョンが3箇所に重複      どれか1つ直すと表示と配布がずれる
T-16-R4  POSIX 固有 API の使用        Windows でテストが AttributeError
T-16-R5  nodeid のパス区切り          静的ガードが Windows で全件誤検出
T-16-R6  韓国語・簡体字の混入         UI/ドキュメントの文が壊れる
==================================== ========================================
"""

from __future__ import annotations

import ast
import re
import signal
import sys
import tomllib
from pathlib import Path

import pytest

from tests._utf8_subprocess import run_module_utf8

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "shiftai"

#: 日本語を出力する CLI サブコマンド（軽いものだけ使う）。
LIGHT_COMMANDS = [["--version"], ["presets"]]


# ---------------------------------------------------------------------------
# T-16-R1 / T-16-R2: CLI の出力がロケールに依存しない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["cp1252", "cp932", "ascii", "latin-1"])
@pytest.mark.parametrize("args", LIGHT_COMMANDS)
def test_CLIは非UTF8ロケールでも終了コード0になる(
    encoding: str, args: list[str]
) -> None:
    """日本語のロケール以外でも全サブコマンドが例外なく終わること。

    修正前は ``PYTHONIOENCODING=cp1252`` の環境で::

        UnicodeEncodeError: 'charmap' codec can't encode characters ...

    が traceback として出て終了コード 1 になっていた。日本語を大量に出力する
    ツールなので、ロケールに依存しないことが運用上の必須条件である。
    """
    import os

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = encoding
    proc = run_module_utf8("shiftai", *args, env=env, timeout=180)
    assert proc.returncode == 0, (
        f"PYTHONIOENCODING={encoding} で失敗した:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "Traceback" not in proc.stderr, (
        f"PYTHONIOENCODING={encoding} で traceback が出ている:\n{proc.stderr}"
    )


def test_CLIは常にUTF8バイト列で出力する() -> None:
    """出力が「日本語を含む UTF-8 として読める」こと。

    出力を読む側（テスト・パイプ・他プログラム）がロケールに依存しない
    ための契約。``presets`` は必ず日本語を含む。
    """
    proc = run_module_utf8("shiftai", "presets", timeout=180)
    assert proc.returncode == 0
    # decode 済みなのでErrors='replace' なら常に通る。
    # 「化け字」が入っていないこと（\ufffd が無いこと）で判定する。
    assert "全国基準" in proc.stdout, "日本語が化けていないこと"
    assert "\ufffd" not in proc.stdout, f"置換文字が混ざっている: {proc.stdout[:300]}"
    assert "\ufffd" not in proc.stderr


def test_force_utf8_streamsはreconfigureを持たないstreamで例外を出さない() -> None:
    """``reconfigure`` を持たない stdout でも落ちないこと。

    ``pytest`` のキャプチャやリダイレクト先では ``sys.stdout`` が
    ``TextIOWrapper`` 以外のオブジェクトになることがある。
    """
    from shiftai.__main__ import _force_utf8_streams

    class _NoReconfigure:
        def write(self, _s: str) -> int:
            return 0

        def flush(self) -> None:
            return None

    class _Raises:
        def reconfigure(self, **_kwargs) -> None:
            raise OSError("this stream is not reconfigurable")

    original = (sys.stdout, sys.stderr)
    try:
        sys.stdout, sys.stderr = _NoReconfigure(), _Raises()  # type: ignore[assignment]
        _force_utf8_streams()  # 例外が出なければよい
    finally:
        sys.stdout, sys.stderr = original


# ---------------------------------------------------------------------------
# T-16-R3: バージョンの二重管理をやめる
# ---------------------------------------------------------------------------


def test_バージョンの唯一のsourceはpyprojectである() -> None:
    """``pyproject.toml`` 以外でバージョンをハードコードしないこと。

    修正前は ``pyproject.toml`` / ``config.APP_VERSION`` /
    ``shiftai.__version__`` の 3 箇所に同じ文字列が書かれており、
    どれか 1 つを直すと「画面の表示」と「配布される版」がずれていた。
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = data["project"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", declared), f"pyproject の version が不正: {declared}"

    # __init__.py / config.py にリテラルとして現れてはならない
    #（代入右辺に __version__ を使う形は許す）。
    for rel in ("__init__.py", "config.py"):
        source = (SRC / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith(("__version__", "APP_VERSION")):
                continue
            assert not re.search(r"""=\s*["']\d+\.\d+\.\d+["']""", stripped), (
                f"src/shiftai/{rel}:{lineno} でバージョンをハードコードしている: {stripped}"
            )


def test_表示用バージョンがpyprojectと一致する() -> None:
    """UI に出す版が pyproject の版と一致すること。"""
    from shiftai import config

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config.APP_VERSION == data["project"]["version"]


def test_version表示が版と一致する() -> None:
    """``--version`` の出力が pyproject の版と一致すること。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    proc = run_module_utf8("shiftai", "--version", timeout=180)
    assert proc.returncode == 0
    assert data["project"]["version"] in proc.stdout


def test_サブパッケージの__all__が解決できる() -> None:
    """``__all__`` に挙げた名前が実際に import できること。

    修正前は ``shiftai/__init__.py`` が ``__all__ = ["domain"]`` と書いて
    いたが ``domain`` を import していなかった。
    """
    import shiftai

    assert shiftai.__all__
    for name in shiftai.__all__:
        assert hasattr(shiftai, name), f"__all__ の {name} が import できない"


# ---------------------------------------------------------------------------
# T-16-R4: POSIX 固有 API を使わない
# ---------------------------------------------------------------------------


def test_signal_SIGALRMをテストが使わない() -> None:
    """``signal.SIGALRM`` / ``signal.alarm`` をテスト内で使わないこと。

    ``SIGALRM`` は POSIX 固有で **Windows には存在しない**。使うと::

        AttributeError: module 'signal' has no attribute 'SIGALRM'

    となり、Windows でそのテストだけ必ず落ちる。修正前は
    ``tests/test_solver_perf.py`` の無限ループ検出が ``SIGALRM`` を使っていた。
    """
    pattern = re.compile(r"\b(signal\.SIGALRM|signal\.alarm)\b")
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        # docstring は「説明」なので、実行される式ノードだけを見る。
        # 行単位で読むと docstring 内の言及に引っかかるため、
        # AST で「どの行が実際のコードか」を判定する。
        doc_lines: set[int] = set()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                doc_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        for lineno, line in enumerate(source.splitlines(), 1):
            if lineno in doc_lines:
                continue
            if pattern.search(line.split("#", 1)[0]):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "POSIX 固有の signal API を使っている（Windows で落ちる）:\n"
        + "\n".join(sorted(set(offenders))[:15])
    )


def test_SIGALRMの有無は検査の前提どおりである() -> None:
    """このテストが動く環境では ``hasattr`` の扱いが前提と一致すること。"""
    # POSIX にはある / Windows には無い、という前提自体が正しいことの確認。
    if sys.platform == "win32":
        assert not hasattr(signal, "SIGALRM"), "Windows に SIGALRM があったら見直す"
    else:
        assert hasattr(signal, "SIGALRM")


# ---------------------------------------------------------------------------
# T-16-R5: nodeid のパス区切り
# ---------------------------------------------------------------------------


def test_静的ガードのnodeidがPOSIX区切りである() -> None:
    """静的ガードが ``\\`` を含む nodeid を作らないこと。

    pytest の nodeid は ``tests/test_x.py::test_y`` の形で常に ``/`` を使う。
    ``str(Path)`` は Windows で ``\\`` になるため、両者を比較すると
    **全件が不一致** になり、ガードが Windows で必ず落ちていた。
    """
    from tests import test_static_guards

    for nodeid in test_static_guards._cbc_nodes():
        assert "\\" not in nodeid, f"nodeid にバックスラッシュが含まれる: {nodeid}"
        assert nodeid.startswith("tests/"), f"nodeid の前置が tests/ ではない: {nodeid}"


def test_収集したnodeidと静的ガードのnodeidが同じ書式である() -> None:
    """実際に収集した nodeid とガード側の nodeid が突き合わせられること。"""
    from tests import test_static_guards

    collected = {nodeid for nodeid, _ in test_static_guards._collect_items()}
    assert collected, "テストが収集できていない"
    cbc = test_static_guards._cbc_nodes()
    assert cbc, "CBC を起動するテストの検出に失敗している"
    unknown = sorted(cbc - collected)
    assert not unknown, (
        "ガードは認識しているが pytest が収集できない nodeid がある（パス区切り不一致）:\n"
        + "\n".join(unknown[:10])
    )


# ---------------------------------------------------------------------------
# T-16-R6: 韓国語・簡体字の混入（ガードの実効性）
# ---------------------------------------------------------------------------


def test_韓国語が混入していない() -> None:
    """日本語の文書にハングル（韓国語）が混ざっていないこと。

    ``test_static_guards.py`` の検査は「簡体字」「ハングル」を検出するが、
    **検査そのものを検査するテストが無い**と、リストを書き換えて
    検査を無効化したことに気づけない。ここではガードのロジックを
    ライブラリとして呼べる形に切り出して、検出能力的を再確認する。
    """
    from tests.test_static_guards import ALLOWED_KANJI, SIMPLIFIED_ONLY

    def detect(text: str) -> list[str]:
        found: list[str] = []
        for line in text.splitlines():
            for char in line:
                if "\uac00" <= char <= "\ud7af" or "\u1100" <= char <= "\u11ff":
                    found.append(f"韓国語 {char}")
                    break
                if char in SIMPLIFIED_ONLY and char not in ALLOWED_KANJI:
                    found.append(f"簡体字 {char}")
                    break
        return found

    # 検査対象は「SIMPLIFIED_ONLY にあり、かつ許可されていない字」。
    # 許可リストにもある字（経・厳など）は「日本語の異体字」として
    # 意図的に通しているので、検出テストには使わない。
    non_allowed = sorted(SIMPLIFIED_ONLY - ALLOWED_KANJI)
    assert non_allowed, "SIMPLIFIED_ONLY に未許可の字が1つもない"
    sample = non_allowed[0]
    assert detect(f"保育補助{sample}が混入している"), (
        f"簡体字「{sample}」を検出できない"
    )
    # 正常な日本語を「韓国語」と誤検出しないこと。
    assert detect("正常系" + chr(0xC815)), "韓国語を検出できない"
    assert not detect("これは正常系です"), "日本語を韓国語と誤検出している"
    assert not detect("(normal) 日本語の文章"), "ascii と日本語を誤検出している"
    assert not detect("0歳児4:1・1歳児5:1"), "正当な日本語を誤検出している"
    assert ALLOWED_KANJI, "許可リストが空だと誤検出が大量に出る"


def test_ソースとドキュメントに韓国語が混ざっていない() -> None:
    """``src`` と ``docs`` と README の実数据进行検査する。"""
    targets = [*SRC.rglob("*.py"), *(ROOT / "docs").rglob("*.md"), ROOT / "README.md"]
    offenders: list[str] = []
    for path in sorted(targets):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any("\uac00" <= c <= "\ud7af" or "\u1100" <= c <= "\u11ff" for c in line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()[:60]}")
    assert not offenders, "韓国語が混入している:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# 補助: 静的ガードが実際に Python として構文が通ること
# ---------------------------------------------------------------------------


def test_テストとソースが構文として正しい() -> None:
    """全ファイルが ``ast.parse`` を通ること（テストが壊れたままでは困るので）。"""
    targets = [*SRC.rglob("*.py"), *(ROOT / "tests").rglob("*.py")]
    for path in sorted(targets):
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - 失敗時にのみ
            pytest.fail(f"{path.relative_to(ROOT)} が構文エラー: {exc}")
