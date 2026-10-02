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

from tests._utf8_subprocess import run_module_utf8, run_utf8

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "shiftai"

#: 日本語を出力する CLI サブコマンド（軽いものだけ使う）。
LIGHT_COMMANDS = [["--version"], ["presets"]]


# ---------------------------------------------------------------------------
# T-16-R1 / T-16-R2: CLI の出力がロケールに依存しない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["cp1252", "cp932", "ascii", "latin-1"])
@pytest.mark.parametrize("args", LIGHT_COMMANDS)
def test_CLIは非UTF8ロケールでも終了コード0になる(encoding: str, args: list[str]) -> None:
    """日本語のロケール以外でも全サブコマンドが例外なく終わること。

    修正前は ``PYTHONIOENCODING=cp1252`` の環境で::

        UnicodeEncodeError: 'charmap' codec can't encode characters ...

    が traceback として出て終了コード 1 になっていた。日本語を大量に出力する
    ツールなので、ロケールに依存しないことが運用上の必須条件である。

    **``force_io_encoding=False`` が無いと空振りになる**（重要）。
    共通ヘルパは既定で子プロセスの ``PYTHONIOENCODING`` を utf-8 に上書きする
    ため、テストが指定した cp1252 がそのまま子上に伝わり、
    「実際にそのロケールで動いた」検証にならない。
    ``test_ロケール指定が子上に伝わること`` がこの前提を別途固定する。
    """
    import os

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = encoding
    proc = run_module_utf8("shiftai", *args, env=env, timeout=180, force_io_encoding=False)
    assert proc.returncode == 0, (
        f"PYTHONIOENCODING={encoding} で失敗した:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "Traceback" not in proc.stderr, (
        f"PYTHONIOENCODING={encoding} で traceback が出ている:\n{proc.stderr}"
    )


def test_ロケール指定が子上に伝わること() -> None:
    """上のテストが本当に指定したロケールで子プロセスを動かしていること。

    共通ヘルパが ``PYTHONIOENCODING`` を上書きしてしまうことに気づかないまま
    「UTF-8 で動くから」で検証が通ってしまっていたため、
    「子が見るロケール」を直接観測して固定する。
    """
    import os

    for encoding in ("cp1252", "cp932"):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = encoding
        proc = run_utf8(
            [sys.executable, "-c", "import sys; print(sys.stdout.encoding)"],
            env=env,
            timeout=120,
            force_io_encoding=False,
        )
        observed = proc.stdout.strip()
        assert observed == encoding, f"子は PYTHONIOENCODING={encoding} ではなく {observed} を見た"


def test_既定では子プロセスをUTF8で走らせる() -> None:
    """既定（``force_io_encoding=True``）では UTF-8 に固定されること。

    日本語出力を cp932 等で読むのは壊れているため、通常の呼び出しは UTF-8 固定。
    """
    proc = run_utf8([sys.executable, "-c", "import sys; print(sys.stdout.encoding)"], timeout=120)
    assert proc.stdout.strip() == "utf-8"


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
    # （代入右辺に __version__ を使う形は許す）。
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
    assert not offenders, "POSIX 固有の signal API を使っている（Windows で落ちる）:\n" + "\n".join(
        sorted(set(offenders))[:15]
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

#: 簡体字ガードが **必ず検出しなければならない** 3 例（positive control）。
#:
#: 簡体字ガードが書かれた最大の動機がまさにこの 3 例で、
#: かつ Round 1 の実測で「許可リストに全部含まれていたため検出できなかった」ことを確認した。
#: リテラルで書くと CJK 文字化けガード（``tests/`` も対象）に引っかかるため、
#: コードポイントから組み立てて定義する。
_POSITIVE_CONTROL_DEFECTS: tuple[str, ...] = (
    "".join(chr(c) for c in (0x4FDD, 0x80B2, 0x88DC, 0x52A9, 0x7ECF, 0x9A8C)),  # 保育補助 + 経 + 験
    "".join(chr(c) for c in (0x4E25, 0x683C, 0x306A, 0x5B9A, 0x54E1, 0x6BD4)),  # 厳 + 格 な 定員比
    "".join(chr(c) for c in (0x4FDD, 0x80B2, 0x4E3B, 0x7BA1, 0x90E8, 0x95E8)),  # 保育主 管 + 門
)


def test_韓国語が混入していない() -> None:
    """日本語の文書にハングル（韓国語）が混ざっていないこと。

    ``test_static_guards.py`` の検査は「簡体字」「ハングル」を検出するが、
    **検査そのものを検査するテストが無い**と、リストを書き換えて
    検査を無効化したことに気づけない。ここではガードのロジックを
    ライブラリとして呼べる形に切り出して、検出能力を再確認する。
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

    # 許可リストに「字形が似ている」だけの理由で字を足すと、
    # 混入が検出できなくなる（実際にその状態で 10 箇所の混入を検出できなかった）。
    # したがって **ガードが書かれていた 3 例**を positive control として固定する。
    #
    # 例そのものは簡体字を含むため、リテラルで書くと
    # CJK 文字化けガードの検査対象（``tests/``）に引っかかる。
    # そこで **コードポイントから組み立てる**。守卫自身は完全にクリーンになる。
    for defect in _POSITIVE_CONTROL_DEFECTS:
        assert detect(f"{defect}が混ざっている"), (
            f"ガードが書かれた実際の混入例「{defect}」を検出できない"
        )

    # 正常な日本語を「韓国語」と誤検出しないこと。
    assert detect("正常系" + chr(0xC815)), "韓国語を検出できない"
    assert not detect("これは正常系です"), "日本語を韓国語と誤検出している"
    assert not detect("(normal) 日本語の文章"), "ascii と日本語を誤検出している"
    assert not detect("0歳児4:1・1歳児5:1"), "正当な日本語を誤検出している"
    assert ALLOWED_KANJI, "許可リストが空だと誤検出が大量に出る"


def test_許可リストは簡体字を大量に含まない() -> None:
    """``ALLOWED_KANJI`` がガードを無効化していないこと。

    Round 1 の査察で、許可リストが簡体字 210 字を含み、
    **ガード自身の docstring が挙げた 3 例が全て通ってしまう**ことが判明した。
    「字形が似ている」だけでは正当化できない
    （簡体字の経 U+7ECF と日本語の経 U+7D4C は **別符号位置**）。
    """
    from tests.test_static_guards import ALLOWED_KANJI, SIMPLIFIED_ONLY

    overlap = ALLOWED_KANJI & SIMPLIFIED_ONLY
    # 実際にコード中で使われている字の数は限定的。過剰に許可しないこと。
    assert len(overlap) <= 20, (
        f"許可リストが簡体字を {len(overlap)} 字含む（ガードが無効化されている）: "
        + "".join(sorted(overlap))
    )


def test_許可リストの字は実際に使われている() -> None:
    """許可した字のうち、使用されていないものは削除できること。

    「今使っていない字を許可する」のは将来の混入を隠すだけなので、
    許可リストが実際の使用に正しく対応していることを固定する。
    """
    from tests.test_static_guards import (
        ALLOWED_KANJI,
        CJK_GUARD_EXEMPT_FILES,
        SIMPLIFIED_ONLY,
    )

    targets = [*SRC.rglob("*.py"), *(ROOT / "docs").rglob("*.md"), ROOT / "README.md"]
    used: set[str] = set()
    for path in sorted(targets):
        rel = path.relative_to(ROOT).as_posix()
        if any(name in rel for name in CJK_GUARD_EXEMPT_FILES):
            continue
        used.update(path.read_text(encoding="utf-8"))

    # SIMPLIFIED_ONLY に入っている許可字だけが検査の対象になる。
    # それらは「実際に使われている」かどうかに意味を持つ。
    suspects = {c for c in ALLOWED_KANJI & SIMPLIFIED_ONLY if c not in used}
    assert not suspects, "許可されているがどこにも使われていない字（削除できる）: " + "".join(
        sorted(suspects)
    )


def test_ソースとドキュメントに韓国語が混ざっていない() -> None:
    """``src`` と ``docs`` と README の実データを検査する。"""
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
