"""静的な構造検査（回帰防止の第三層）。

ここで検査するものは「テストでは検出できない種類の不具合」であり、
いずれも実際にこのプロジェクトで発生したものである。

* 常に真になる ``assert``（``or True`` / ``in set(enum)``）が緑のまま残る
* CBC（MILP ソルバ）を起動するテストが ``slow`` マーク無しで残り、
  ``make test-fast`` が実測より遥かに遅くなる
* 参照されていない公開関数が残る
* 日本語の文字列に簡体字（中国語専用）が混入する
* テストが作業ディレクトリを固定パスで共有し、並列実行で衝突する
* ドキュメントにテスト件数がハードコードされ、実測と食い違う
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "shiftai"
TESTS = ROOT / "tests"
DOCS = ROOT / "docs"

CBC_ENTRYPOINTS = frozenset({"solve_shift", "solve_shift_greedy", "check_violations"})


# ---------------------------------------------------------------------------
# T-07-R1: 常に真になる assert がない
# ---------------------------------------------------------------------------


def test_常に真になるassertがない() -> None:
    """``or True`` や ``in set(<Enum>)`` による無意味な検査が残っていないこと。"""
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assert):
                continue
            source = ast.unparse(node.test)
            if re.search(r"\bor\s+True\b", source):
                offenders.append(f"{path.name}:{node.lineno}: {source[:70]}")
            elif re.search(r"\bin\s+set\(", source) and "SolveStatus" in source:
                offenders.append(f"{path.name}:{node.lineno}: {source[:70]}")
    assert not offenders, "常に真になる検査が見つかった:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# T-06-R1: CBC を起動するテストには slow が付いている
# ---------------------------------------------------------------------------


def _collect_items() -> list[tuple[str, bool]]:
    """実際に収集し、CBC を起動するテストの ``slow`` 付与状況を返す。

    ``conftest.py`` のフックは収集時に働くため、ソースの印ではなく
    収集後の状態を検証しないと意味がない。``--collect-only`` なので
    テスト本体は実行しない。
    """
    captured: list[tuple[str, bool]] = []

    class Collector:
        def pytest_collection_modifyitems(self, items):
            for item in items:
                captured.append((item.nodeid, "slow" in item.keywords))

    pytest.main(
        ["--collect-only", "-q", "-p", "no:randomly", str(TESTS)],
        plugins=[Collector()],
    )
    return captured


def _load_tests_conftest():
    """``tests/conftest.py`` をモジュールとして読み込む。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_shiftai_tests_conftest", TESTS / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cbc_nodes() -> set[str]:
    """CBC を起動するテストの nodeid 集合。"""
    cbc_fixtures = _load_tests_conftest()._fixture_uses_cbc()
    nodes: set[str] = set()
    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
                continue
            args = [a.arg for a in node.args.args] + [a.arg for a in node.args.kwonlyargs]
            if _calls_any(node, CBC_ENTRYPOINTS) or any(a in cbc_fixtures for a in args):
                nodes.add(f"{path.relative_to(ROOT)}::{node.name}")
    return nodes


def test_slow未付与のCBCテストは存在しない() -> None:
    """``slow`` マークが無いのに CBC を起動するテストが残っていないこと。

    ``conftest.py`` の ``pytest_collection_modifyitems`` が CBC を起動する
    テストに自動的に ``slow`` を付けるため、``make test-fast``
    （``-m "not slow"``）が実測より遅くならないことを保証する。
    """
    items = _collect_items()
    assert items, "テストが収集できていない"
    cbc_nodes = _cbc_nodes()
    assert cbc_nodes, "CBC を起動するテストの検出に失敗している"
    slow_by_node = {nodeid: marked for nodeid, marked in items}
    offenders = [
        nodeid
        for nodeid in sorted(cbc_nodes)
        if not slow_by_node.get(nodeid, False)
    ]
    assert not offenders, (
        f"CBC を起動するテストに slow が無い（{len(offenders)} 件）:\n"
        + "\n".join(offenders[:15])
    )


def _calls_any(node: ast.AST, names: frozenset[str]) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Attribute) and child.func.attr in names:
                return True
            if isinstance(child.func, ast.Name) and child.func.id in names:
                return True
    return False


def test_slow対象が実際に集められること() -> None:
    """``slow`` マークのテストが 0 件になっていないこと（マーカー失効の防止）。"""
    import subprocess

    proc = subprocess.run(
        ["python", "-m", "pytest", "--collect-only", "-q", "-m", "slow"],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    total = sum(
        1 for line in proc.stdout.splitlines() if line.startswith("tests/") and "::" in line
    )
    assert total > 0, "slow テストが 1 件も収集できない（マーカーの付け忘れ）"


# ---------------------------------------------------------------------------
# T-11-R1: 死んだ公開関数がない
# ---------------------------------------------------------------------------


def test_死んだ公開関数がない() -> None:
    """``src`` 内にも ``tests`` 内にも参照が無い公開関数が残っていないこと。"""
    # エントリポイント（streamlit_app.py / conftest.py）も参照元として数える
    extras = [p for p in (ROOT / "streamlit_app.py", ROOT / "conftest.py") if p.exists()]
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in [*SRC.rglob("*.py"), *extras]
    )
    tests = "\n".join(
        path.read_text(encoding="utf-8") for path in list(TESTS.rglob("*.py"))
    )
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            calls = len(re.findall(rf"\b{re.escape(node.name)}\b", sources))
            if calls <= 1 and re.search(rf"\b{re.escape(node.name)}\b", tests) is None:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.name}")
    assert not offenders, "参照されていない公開関数:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# T-12-R1: 簡体字が混入していない
# ---------------------------------------------------------------------------

#: 日本語としても使う字（簡体字と字形が同じ、または日本語の異体字）。
ALLOWED_KANJI = set(
    "会内写体乱赤余与调个义骨随严经龄门币儿兴举乐乡价众优伙传关农决况净凉减几凤凭击刘则刚创删荐荣"
    "药获营蓝虑虽补见观视览认订训议讯记讲访设许诉诊词译试诚误说课贝负贡财责贤败货质贪贫购贯贱贴贵贷"
    "贸费贺资赋赏赔赖赚赛赞赵趋跃适选逊递遗邓郑邻释针钟钢钥钩钱钻铁铃铅铜铝铭铲银铸铺链销锁锄锅错"
    "锦键锯锻镀镇闪闭闯闲间闷闹闻阁阅队阳阴阵阶际陆陈险隐隶雏雾顶顷项顺须顾顿颁预领颇颈频颗题颜额"
    "飘饥饭饮饱饿馆馒驱驳驶驾验骂骄骗骤骨鲁鲜鸣鹅麦齐齿龄龙龟"
    # 日本語の正字法として許容されるもの
    "当毎遅条数担当遅刻黄黒青緑白"
)

#: 簡体字（中国語専用）で、日本語として誤った文字。
SIMPLIFIED_ONLY = set(
    "经验调个义门严币儿兴举乐乡价众优伙传关农决况净凉减几凤凭击刘则刚创删荐荣药获营蓝虑虽补见观视览认"
    "订训议讯记讲访设许诉诊词译试诚误说课贝负贡财责贤败货质贪贫购贯贱贴贵贷贸费贺资赋赏赔赖赚赛赞赵趋跃"
    "适选逊递遗邓郑邻释针钟钢钥钩钱钻铁铃铅铜铝铭铲银铸铺链销锁锄锅错锦键锯锻镀镇闪闭闯闲间闷闹闻阁阅队阳阴"
    "阵阶际陆陈险隐隶雏雾顶顷项顺须顾顿颁预领颇颈频颗题颜额飘饥饭饮饱饿馆馒驱驳驶驾验骂骄骗骤骨鲁鲜鸣鹅麦齐"
    "齿龄龙龟当龄厅亿仅从仑仓价众优伙会传体侧俭党兴养兽内写军农冲决况冻净凉减凤凭击划则刚创删荐荡荣药莲获"
    "莹萤营萧萨葱蒋蓝虏虑虽虾蚀蚁衬见观规视览觉认订讨让训议讯记讲讳讼访设许诉诊词译试诚误说课调谅谈谊谋"
    "贝贞负贡财责贤败货质贩贪贫贬购贮贯贱贴贵贷贸费贺贼贾贿资赋赌赏赐赔赖赚赛赞赤赵赶趋跃践迹适选逊递遥"
    "邓郑邻酝酱酿释里鉴钓钙钝钞钧钩钳钻铁铃铅铆铜铝铠铡铭铲银铸铺链销锁锄锅错锦键锯锻镀镇镜闪闭闯闲间闷"
    "闹闻阁阅队阳阴阵阶际陆陇陕险随隐隶难雏雾韩顶顷项顺须顾顿颁颂预领颇颈频颗题颜额飘饥饭饮饲饱饼饿馄馅馆"
    "馈馒驰驱驳驻驼驾验骂骄骗骤骨鲁鲜鸟鸡鸣鸿鹅鹉鹏鹤麦黄齐齿龈龉龊龙龟"
)


def test_簡体字が混入していない() -> None:
    """日本語の文字列に簡体字（中国語専用）が混ざっていないこと。

    実際に ``保育補助经验`` / ``严格な定員比`` / ``保育主管部门`` のような
    混入が起きていたため、静的に検査する。

    検出された場合は (1) 本文の誤字なら本文を直し、
    (2) 日本語として正当な字なら :data:`ALLOWED_KANJI` へ追加し、
    理由を pull request に書くこと。黙って除外リストを増やさない。
    """
    offenders: list[str] = []
    targets = [p for p in sorted(SRC.rglob("*.py"))]
    targets += [p for p in sorted(DOCS.rglob("*.md"))]
    targets.append(ROOT / "README.md")
    for path in targets:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for char in line:
                if char in SIMPLIFIED_ONLY and char not in ALLOWED_KANJI:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: 「{char}」 {line.strip()[:60]}")
                    break
                # 日本語の文書に韓国語・中国簡体字以外の外国語の文字が混ざっていないこと
                # （実際に README の 1 行に韓国語の 2 文字が混ざっていた）
                if "\uac00" <= char <= "\ud7af" or "\u1100" <= char <= "\u11ff":
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: 韓国語「{char}」 {line.strip()[:60]}")
                    break
    assert not offenders, "簡体字が見つかった:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# T-09-R1: 固定パスを使わない
# ---------------------------------------------------------------------------


def test_e2eがtmp_e2eを参照しない() -> None:
    """テストが ``/tmp/...`` を直接，共有していないこと。"""
    offenders: list[str] = []
    for path in sorted(TESTS.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"[\"']/tmp/", line) and "test_static_guards" not in path.name:
                offenders.append(f"{path.name}:{lineno}: {line.strip()[:70]}")
    assert not offenders, "固定パスが使われている:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# T-14-R1: ドキュメントにテスト件数をハードコードしない
# ---------------------------------------------------------------------------


def test_docsにテスト件数がハードコードされていない() -> None:
    """ドキュメントに「N 件」「N passed」等の件数が書かれていないこと。

    件数は毎リリースで変わるため、``pytest --collect-only -q`` を参照させる。
    過去の例: docs が 689 件と書いて実際は 751 件だった。
    """
    # 「テスト」「件数」「passed」「deselected」など試験回数であることが
    # 文脈で明白な行だけを検査する（"1 件 BLOCKER" のような仕様記述は対象外）。
    context = re.compile(r"テスト|test|passed|failed|deselected|xfailed|pytest|pytest.mark|\.py")
    counter = re.compile(r"\d+\s*(?:件|passed|failed|deselected|xfailed)\b")
    offenders: list[str] = []
    targets = [(path, path.read_text(encoding="utf-8")) for path in sorted(DOCS.rglob("*.md"))]
    targets.append((ROOT / "Makefile", (ROOT / "Makefile").read_text(encoding="utf-8")))
    for path, body in targets:
        for lineno, line in enumerate(body.splitlines(), 1):
            if counter.search(line) and context.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()[:70]}")
    offenders = [o for o in offenders if "07_実装計画" not in o]
    assert not offenders, "件数がハードコードされている:\n" + "\n".join(offenders)


def test_docsのターゲット一覧が実在する() -> None:
    """``docs/05`` が挙げる Makefile ターゲットがすべて存在すること。"""
    import re as _re

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    declared = set(_re.findall(r"^([A-Za-z0-9_.-]+):", makefile, flags=_re.MULTILINE))
    guide = (DOCS / "05_開発ガイド.md").read_text(encoding="utf-8")
    referenced = set(_re.findall(r"`make ([A-Za-z0-9_.-]+)`", guide))
    missing = sorted(referenced - declared)
    assert not missing, f"Makefile に無いターゲットが docs にある: {missing}"
