"""ドキュメントと実装の一致を検証する回帰テスト（R1-G）。

**なぜ必要か**

README や ``docs/`` は人が読むため、実装から乖離してずれる。
実際に次の drift が検出された:

* README が給与計算の二重控除を「制約」として記述していた
  （``net_hours = max((worked_min - break_min) / 60.0, 0.0)`` という
  **存在しない行を引用**していた。実実装は休憩を引かない）
* README が GUI を「5 タブ」と記述していた（既定は 3 タブ）
* ``docs/05`` が削除済みの定数 ``TIMEOUT`` を公開OURCEとして記述していた

ここで **実装から自動的に検証できる主張** を固定し、
次の同类の drift を検出できるようにする。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
GUIDE = ROOT / "docs" / "05_開発ガイド.md"
SPEC = ROOT / "docs" / "04_データ仕様.md"
SOLVER_SPEC = ROOT / "docs" / "02_ソルバ仕様.md"
PLAN08 = ROOT / "docs" / "08_実装計画_公平性・即時検証・CSV連携.md"
REQUIREMENTS = ROOT / "requirements.txt"
REQUIREMENTS_DEV = ROOT / "requirements-dev.txt"
SRC = ROOT / "src" / "shiftai"

DOC_FILES = sorted(
    [README, GUIDE] + sorted((ROOT / "docs").glob("*.md")) + sorted((ROOT / "gas").glob("*.md"))
)


def _doc_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ソースコード引用の実在性
# ---------------------------------------------------------------------------


def test_ドキュメントが引用するコード片が実在する() -> None:
    """`` `ファイル.py` `` 形式の引用がリポジトリ内のどこかに実在すること。

    README が実在しないコードを引用していた（``net_hours = ...``）。
    ここでは「ファイル名の引用」自体を検証する。
    検索対象は ``src/`` ``tests/`` ``gas/`` およびリポジトリ直下。
    """
    search_roots = [SRC, ROOT / "tests", ROOT / "gas"]
    existing: set[str] = {p.name for r in search_roots for p in r.rglob("*.py")}
    existing |= {p.name for p in ROOT.glob("*.py")}

    offenders: list[str] = []
    for path in DOC_FILES:
        for lineno, line in enumerate(_doc_text(path).splitlines(), 1):
            for match in re.finditer(r"`([a-z_0-9]+\.py)`", line):
                filename = match.group(1)
                if filename not in existing:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {filename} が存在しない")
    assert not offenders, "存在しないファイルを引用している:\n" + "\n".join(sorted(set(offenders)))


def test_給与計算のコード引用が実装と一致する() -> None:
    """``net_hours`` の定義がドキュメントの説明と矛盾しないこと。

    README は「実働時間は休憩を差し引いた時間」と書いていたが、
    実装は ``max(worked_min / 60.0, 0.0)`` で休憩を引かない（T-05 の意図）。
    """
    exporter = (SRC / "exporter.py").read_text(encoding="utf-8")
    match = re.search(r"net_hours\s*=\s*max\(([^)]+)\)", exporter)
    assert match, "exporter.py に net_hours の定義が見つからない"
    expr = match.group(1)
    assert "break_min" not in expr, (
        f"net_hours が休憩を引いている: {expr}（二重控除・T-05 の設計と矛盾）"
    )
    # README がこの式を引用しているなら、実際の式と一致すること
    readme = _doc_text(README)
    quoted = re.search(r"net_hours\s*=\s*max\(([^)]+)\)", readme)
    if quoted:
        assert quoted.group(1).replace(" ", "") == expr.replace(" ", ""), (
            f"README の引用と実装が不一致:\n  README: {quoted.group(1)}\n  実装   : {expr}"
        )


def test_給与説明が二重控除しないことを明記している() -> None:
    """README が二重控除の誤解を招く書き方をしていないこと。"""
    readme = _doc_text(README)
    assert "二重控除" in readme, (
        "README に休憩の二重控除についての説明がない（利用者が二重控除と誤解する）"
    )
    assert "差し引いた" not in readme.split("## 9.")[-1][:4000], (
        "README §9 に『差し引いた』という古い記述が残っている"
    )


def test_データ仕様書の実働時間も二重控除していない() -> None:
    """``docs/04`` も同じ式を同じ意味で記載していること。

    修正前: ``docs/04`` だけが ``max(総勤務時間 - 総休憩時間, 0)`` と
    旧式の二重控除を記載したままで、README と実装（``exporter.py``）と矛盾していた。
    どちらを読んでも二重控除しないことが分かるようにする。
    """
    spec = _doc_text(SPEC)
    assert "実働時間" in spec, "docs/04 に実働時間の列定義がない"
    offenders = [
        f"docs/04:{i}: {line.strip()}"
        for i, line in enumerate(spec.splitlines(), 1)
        if "実働時間" in line and "総勤務時間 - 総休憩時間" in line
    ]
    assert not offenders, "docs/04 は実働時間から休憩を引いている（旧式の二重控除）:\n" + "\n".join(
        offenders
    )


def test_データ仕様書の引用関数と定数が実在する() -> None:
    """``docs/04`` が「どの関数/定数の定義か」を引用しているなら実在であること。

    修正前: ``violations.csv`` の生成関数として ``__main__._violations_dataframe``
    を引用していたが、その関数は存在しなかった（実際は ``exporter.violations_dataframe``）。
    """
    spec = _doc_text(SPEC)
    # ``ファイル.py:行`` 形式の引用すべてを実在する行に紐づける
    source_lines: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        source_lines[path.name] = path.read_text(encoding="utf-8").splitlines()

    offenders: list[str] = []
    for lineno, line in enumerate(spec.splitlines(), 1):
        for match in re.finditer(r"`([a-z_0-9]+\.py):(\d+)`", line):
            fname, num = match.group(1), int(match.group(2))
            lines = source_lines.get(fname)
            if lines is None:
                offenders.append(f"docs/04:{lineno}: {fname} が存在しない")
            elif num > len(lines):
                offenders.append(
                    f"docs/04:{lineno}: {fname} は {len(lines)} 行しか無いのに :{num} を引用"
                )
    assert not offenders, "存在しない位置を引用しています:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# タブ構成
# ---------------------------------------------------------------------------


def _tab_counts() -> tuple[int, int]:
    """AppTest を実際に走らせて（シンプル / 上級者）のタブ数を取得する。"""
    app_test = pytest.importorskip("streamlit.testing.v1")
    at = app_test.AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=180).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    simple = len(at.tabs)
    at.toggle(key="ui_simple_mode").set_value(False).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return simple, len(at.tabs)


def test_READMEのタブ数の記述が実装と一致する() -> None:
    """README が GUI のタブ構成を正しく記述していること。

    修正前: README は「5 タブ」とだけ書いていたが、
    既定は 3 タブ（シンプルモード）だった。
    """
    simple, advanced = _tab_counts()
    assert simple == 3, f"シンプルモードが {simple} タブ（3 を期待）"
    assert advanced == 5, f"上級者モードが {advanced} タブ（5 を期待）"

    readme = _doc_text(README)
    assert "3 タブ" in readme, "README に 3 タブ（シンプルモード）の記述がない"
    assert "5 タブ" in readme, "README に 5 タブ（上級者モード）の記述がない"


# ---------------------------------------------------------------------------
# 制約の硬さ（ハード / ソフト）の記述と実装の整合
# ---------------------------------------------------------------------------

#: README が「ハード制約」と称してはいけない項目。
#: これらはすべて ``solver.py`` では**ソフトペナルティ**（slack を目的関数に載せる）
#: であり、ハード制約にすると配置基準を満たせない解しか出なくなる。
#: 修正前: README がこれらを「契約（…）をハード制約として扱います」と記していた。
SOFT_ONLY_ITEMS: tuple[str, ...] = (
    "週最大出勤日数",
    "連続勤務上限",
    "連続勤務日数",
    "週契約時間",
)


def _solver_docstring_constraint_sections() -> tuple[str, str]:
    """``solver.py`` の docstring から「ハード」「ソフト」の箇条書きを取り出す。

    実際の docstring は 1 項目 1 行ではなく、
    ``* ハード: セル状態の排他 / 希望休 / …`` のように**1 行にまとめて**書かれている。
    したがって ``* ハード:`` / ``* ソフト:`` で始まる行をそのまま収集する。
    """
    src = (SRC / "solver.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    doc = ast.get_docstring(tree) or ""
    hard = [ln for ln in doc.splitlines() if ln.strip().startswith("* ハード:")]
    soft = [ln for ln in doc.splitlines() if ln.strip().startswith("* ソフト:")]
    return "\n".join(hard), "\n".join(soft)


def test_READMEはソフト制約項目をハード制約と書いていない() -> None:
    """``週最大出勤日数`` などを「ハード制約」と称していないこと。

    修正前: README 冒頭が「契約（週契約時間・1日上限・週最大出勤日数・連続勤務上限）を
    **ハード制約**として扱います」と記していたが、実装は
    ``solver.py`` の ``consecutive_day_penalty`` / 週勤務日数の slack であり、
    週契約時間は MILP 制約ですらない（``_staff_signature`` の同一視のみ）。
    """
    readme_lines = _doc_text(README).splitlines()
    offenders: list[str] = []
    for lineno, line in enumerate(readme_lines, 1):
        if "ハード制約" not in line:
            continue
        for item in SOFT_ONLY_ITEMS:
            if item in line:
                offenders.append(f"README.md:{lineno}: {item} を「ハード制約」と記載")
    assert not offenders, "ソフト制約の項目をハード制約と書いています:\n" + "\n".join(offenders)


#: 「ソフトペナルティ一覧に載せるべき項目」→ 表記のゆれ。
#: 同じ設定でも呼び名が複数あるので、どれか 1 つでも書かれていればよい。
#: 例: 連続勤務日数は「連続勤務日数」「連続勤務上限」のどちらの書き方でも可。
SOFT_LIST_REQUIRED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("連続勤務日数", ("連続勤務日数", "連続勤務上限")),
    ("週の最大出勤日数", ("週の最大出勤日数", "週最大出勤日数")),
)


def _soft_penalty_section(text: str) -> str:
    """README の「ソフトペナルティ」記載部分（次の見出しまで）を取り出す。"""
    marker = "**ソフトペナルティ**"
    if marker not in text:
        return ""
    collected: list[str] = []
    for line in text.split(marker, 1)[1].splitlines()[1:]:
        if line.startswith("#"):  # 次の見出しで打ち切る
            break
        collected.append(line)
    return "\n".join(collected)


def test_READMEはソフトペナルティ一覧を明示している() -> None:
    """ソフト制約であることを、読める形で列挙していること。

    ハード制約との区別が読めないと、利用者は「全部ハードだ」と誤解する。
    """
    readme = _doc_text(README)
    assert "**ソフトペナルティ**" in readme, (
        "README に「ソフトペナルティ」の区分がない（ハード制約との区別が読めない）"
    )
    section = _soft_penalty_section(readme)
    assert section.strip(), (
        "「ソフトペナルティ」の記載が空（見出し 直後に内容が無い、または次の見出しが無い）"
    )
    missing = [
        label for label, spellings in SOFT_LIST_REQUIRED if not any(s in section for s in spellings)
    ]
    assert not missing, (
        f"ソフトペナルティ一覧に必要な項目が無い: {missing}"
        f"（{[(label, spellings) for label, spellings in SOFT_LIST_REQUIRED]} のいずれか）"
    )


def test_週契約時間はMILP制約ではない() -> None:
    """``Contract.weekly_hours`` が ``solver.py`` のどこで参照されているかを固定する。

    現在は対称性除去（``_staff_signature``）のためだけに使われており、
    ハード制約としては存在しない。もし今後 ``_hard_ge`` / ``_hard_le`` へ
    組み込まれたら、このテストが落ちるので README の記述も更新すること。
    """
    src = (SRC / "solver.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    users: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr == "weekly_hours":
                users.add(node.name)
                break
    assert users == {"_staff_signature"}, (
        "solver.py で Contract.weekly_hours の参照先が想定と違う: "
        f"{sorted(users)}（_staff_signature のみ想定）。"
        "制約が増えたら README / docs/02 の記述も要更新"
    )


def test_ソルバdocstringがハードとソフトを分類している() -> None:
    """``solver.py`` の docstring が ハード/ソフト の分類を明文化していること。

    分類がなくなると、どの項目が検査で警告されるだけなのか読めなくなる。
    """
    hard, soft = _solver_docstring_constraint_sections()
    assert "セル状態の排他" in hard, f"ハード制約の定義が読めない: {hard!r}"
    assert "希望休" in hard
    for item in ("連続勤務日数", "週の勤務日数"):
        assert item in soft, f"{item} がソフト制約に分類されていない: {soft!r}"


# ---------------------------------------------------------------------------
# docs/05: 削除済みの定数・実装と不一致の設定
# ---------------------------------------------------------------------------


def test_開発ガイドが削除済みの定数を参照していない() -> None:
    """``tests/conftest.py`` に存在しない定数を docs/05 が公開SOURCEとして"+
    "記載していないこと。"""
    guide = _doc_text(GUIDE)
    conftest_src = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    # ``conftest.py`` の ``__all__`` に含まれる名前を取得
    tree = ast.parse(conftest_src)
    exported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            exported = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    # TIMEOUT などが残っていないこと（削除済み）
    assert "TIMEOUT" not in guide, (
        "docs/05 に削除済みの TIMEOUT 定数の記載がある（T-11 で削除済み）"
    )
    assert exported, "tests/conftest.py に __all__ が無い"


def test_開発ガイドのaddoptsが実際の設定と一致する() -> None:
    """docs/05 が示す pytest 設定が pyproject.toml と一致すること。"""
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    actual = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", "")
    guide = _doc_text(GUIDE)
    quoted = re.search(r'addopts\s*=\s*"([^"]*)"', guide)
    assert quoted, "docs/05 に addopts の記載がない"
    assert quoted.group(1) == actual, (
        f"docs/05 の addopts が pyproject.toml と不一致:\n"
        f"  docs/05: {quoted.group(1)}\n"
        f"  実際   : {actual}"
    )


def test_開発ガイドのCLI既定値が実際のargparseと一致する() -> None:
    """``docs/05`` §5.2 のサブコマンド別オプション表が ``build_parser`` と一致すること。

    サブコマンドごとに表を検証する。表は節見出しで区切っており、
    ``#### `compliance` のオプション`` と
    ``**`shiftai solve` の主なオプション**`` を起点に取る。
    節をまたいで収集すると、別サブコマンドのオプションを
    「そのサブコマンドに存在しない」と誤判定するため節で切り分ける。

    修正前: ``docs/05`` が
    * ``--start`` / ``--end`` を「今日〜今日+6 日」と記載（実際は ``None`` で、
            園児 CSV の日付から自動決定する）
          * ``--facility`` を「なし」と記載（実際は ``あさひ保育園``）
          * ``--closed`` を「カンマ区切り」と記載（実際は ``action="append"`` の繰り返し）
          * ``--patterns`` / ``--relax`` / ``--diagnose`` / ``--verbose`` を記載していない
        とずれていた。表の行が実装の既定値から外れる状態を許さない。
    """
    import sys as _sys

    _sys.path.insert(0, str(SRC))
    try:
        from shiftai.__main__ import build_parser
    finally:
        _sys.path.remove(str(SRC))

    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if getattr(action, "choices", None) and "solve" in action.choices
    )

    guide = _doc_text(GUIDE)
    section = guide[guide.index("### 5.2 CLI サブコマンド") :]

    #: 節見出し → その配下の表が属するサブコマンド。
    anchors: tuple[tuple[str, str], ...] = (
        (r"^####\s+`compliance`", "compliance"),
        (r"^\*\*`shiftai solve`", "solve"),
    )

    lines = section.splitlines()
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        for pattern, name in anchors:
            if re.match(pattern, line.strip()):
                current = name
                break
        else:
            if current is not None:
                sections.setdefault(current, []).append(line)

    assert "solve" in sections, "docs/05 §5.2 に solve のオプション表が見つからない"
    assert "compliance" in sections, "docs/05 §5.2 に compliance のオプション表が見つからない"

    offenders: list[str] = []
    for name, rows in sections.items():
        command = subparsers.choices[name]
        actual: dict[str, object] = {}
        for action in command._actions:
            if not action.option_strings or action.dest == "help":
                continue
            for opt in action.option_strings:
                actual[opt] = action.default
        documented: set[str] = set()
        for line in rows:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not cells:
                continue
            for opt in re.findall(r"`(--[a-z-]+)`", cells[0]):
                documented.add(opt)
                if opt not in actual:
                    offenders.append(f"docs/05: {opt} は {name} に存在しない")
        for opt in sorted(set(actual) - documented):
            offenders.append(f"docs/05: {name} の {opt} (default={actual[opt]!r}) が未記載")

    assert not offenders, "オプション表と argparse が不一致:\n" + "\n".join(offenders)


def test_開発ガイドのテスト一覧にプレースホルダが残っていない() -> None:
    """``docs/05`` のテスト一覧に ``test_XX.py`` のような流失した名前が残っていないこと。

    ``docs/09`` R1-G で指摘されつつ未修正だった項目。実際のファイル名に直す。
    """
    guide = _doc_text(GUIDE)
    offenders = [
        f"docs/05:{i}: {line.strip()}"
        for i, line in enumerate(guide.splitlines(), 1)
        if "test_XX" in line
    ]
    assert not offenders, "テスト一覧が placeholder（test_XX.py）のまま:\n" + "\n".join(offenders)


def test_モデル規模の記述がドキュメント間で一致する() -> None:
    """モデル規模（変数/制約数）の記述が、ドキュメント全体で 1 つの値になっていること。

    修正前: ``docs/02`` が「変数 13,845 / 制約 11,452」、
    ``docs/05`` が「変数 13,873 / 制約 11,536」と**互いに矛盾**して記載していた。
    しかもどちらも現在の実装では再現できなかった（時間帯区分の統合で規模が変わった）。

    正は ``docs/02_ソルバ仕様.md`` §2.7。他はここを参照し、数値を直接書かない。
    """
    spec = _doc_text(SPEC)
    guide = _doc_text(GUIDE)
    solver_spec = _doc_text(SOLVER_SPEC)

    def _counts(text: str) -> set[tuple[int, int]]:
        """「変数 N 個 / 制約 M 本」形式の (N, M) を全部取り出す。

        ドキュメントでは太字化や「変数」「制約」の語が多少異なるため、
        「桁区切りの 2 数が / で隣り合う」ことだけを見る。
        """
        found: set[tuple[int, int]] = set()
        number = r"\d{1,3}(?:,\d{3})+"
        # 区切りは "/", "／", "・", "と" のどれかが実際に使われている。
        # 前後は日本語や太字記号が入りうるので、数字以外を許す。ただし
        # **行をまたがない**（表の 2 行目が別の数値に見えるのを防ぐ）。
        # （\W は Python の Unicode モードでは漢字を word 扱いして match しない）
        sep = r"(?:/|／|・|と)"
        gap = r"[^\d\n]"
        pattern = rf"({number}){gap}{{0,16}}?{sep}{gap}{{0,16}}?({number})"
        for match in re.finditer(pattern, text):
            found.add((int(match.group(1).replace(",", "")), int(match.group(2).replace(",", ""))))
        return found

    canonical = _counts(solver_spec)
    assert canonical, (
        "docs/02 §2.7 に「変数 N 個 / 制約 M 本」の記述がない（モデル規模の正となるべき場所）"
    )
    # docs/02 自身は 1 値に揃っていること（複数あれば内訳と矛盾している疑い）
    assert len(canonical) == 1, f"docs/02 内でモデル規模が割れている: {sorted(canonical)}"

    for name, text in (("docs/02", spec), ("docs/05", guide)):
        stray = _counts(text) - canonical
        assert not stray, (
            f"{name} が docs/02 §2.7 と違うモデル規模を書いている: {sorted(stray)}"
            "（docs/02 §2.7 を正として、参照か同値にすること）"
        )


def test_モデル規模の内訳が合計に一致する() -> None:
    """``docs/02`` §2.7 の変数グループ内訳の合計が、tour 合計の値と一致すること。

    内訳表は測定値なので、合計だけを直すとずれる。ここでは
    表の数値を合計して、合計が 1 つの整数に収まることを確認する。
    """
    solver_spec = _doc_text(SOLVER_SPEC)
    section = solver_spec.split("### 2.7", 1)
    assert len(section) == 2, "docs/02 に 2.7 節がない"
    # 2.7 節は「### 再現手順」という下位見出しを含むため、
    # 次の **2 階層** 見出し（``## ``）で切る。
    body = section[1].split("\n## ", 1)[0]

    total_match = re.search(r"\*\*変数 ([\d,]+) 個 / 制約 ([\d,]+) 本\*\*", body)
    assert total_match, "docs/02 §2.7 に太字の合計行がない"
    declared = int(total_match.group(1).replace(",", ""))

    rows = re.findall(r"^\|\s*`([^`]+)`[^|]*\|\s*([^|]*?)\s*\|\s*$", body, re.MULTILINE)
    rows = [(name, raw) for name, raw in rows if re.search(r"\d", raw)]
    assert rows, "docs/02 §2.7 に内訳表がない"

    total = 0
    for _, raw in rows:
        total += sum(int(part.replace(",", "")) for part in re.findall(r"[\d,]+", raw))

    assert total == declared, (
        f"docs/02 §2.7 の内訳合計 {total} が宣言された合計 {declared} と一致しない"
        "（再計測して表を直すこと。再現手順は同節内）"
    )


def test_drop_groupsのキー一覧が実装と一致する() -> None:
    """``docs/02`` の ``drop_groups`` キー表が ``solver.py`` と一致すること。

    修正前: docs/02 は「配置基準（``coverage``）は対象外」と記していたが、
    ``solver.py:1273`` で実際に ``"coverage" not in drop_groups`` として
    分岐していた（＝外せる）。同様に ``fairness`` も表に一圈載していなかった。
    """
    solver_src = (SRC / "solver.py").read_text(encoding="utf-8")
    implemented = set(re.findall(r'"(\w+)"(?:\s+not)?\s+in\s+drop_groups', solver_src))
    assert implemented, "solver.py で drop_groups の分岐が見つからない"

    body = _doc_text(SOLVER_SPEC).split("が受け付けるキー:", 1)
    assert len(body) == 2, "docs/02 に drop_groups のキー表がない"
    # 見出しの直後に空行が入っているので、テーブル行だけを拾う。
    documented = set(re.findall(r"^\|\s*`(\w+)`\s*\|", body[1], re.MULTILINE))
    assert documented, "docs/02 の drop_groups キー表が空"

    undocumented = implemented - documented
    assert not undocumented, (
        "drop_groups を受け付けるキーが docs/02 に無い: "
        f"{sorted(undocumented)}（実装は受け付けている）"
    )
    phantom = documented - implemented
    assert not phantom, (
        f"docs/02 が受け付けると書くが実装に無い drop_groups キー: {sorted(phantom)}"
    )


def test_UIの重みスライダー数がドキュメントと一致する() -> None:
    """``docs/02`` §4.3 のスライダー項目表が ``WEIGHT_WIDGETS`` と一致すること。

    修正前: docs/02 は「スライダーで調整できるのは 5 項目だけ」と記載したが、
    ``ui.state.WEIGHT_WIDGETS`` は 8 項目。``fairness_*`` の 3 項目は
    実装にあるのにドキュメントから消えていた。
    """
    import sys

    sys.path.insert(0, str(SRC))
    try:
        from shiftai.ui.state import WEIGHT_WIDGETS
    finally:
        sys.path.remove(str(SRC))

    names = tuple(entry[0] for entry in WEIGHT_WIDGETS)
    assert names, "WEIGHT_WIDGETS が空"

    body = _doc_text(SOLVER_SPEC).split("UI にスライダーとして出るのは", 1)
    assert len(body) == 2, "docs/02 §4.3 にスライダー数の記述がない"
    # 8 項目 と 明記されているか
    section = _doc_text(SOLVER_SPEC).split("### 4.3", 1)[1].split("\n## ", 1)[0]
    declared = re.search(r"\*\*(\d+) 項目\*\*", section)
    assert declared, "docs/02 §4.3 にスライダー数が明示されていない"
    assert int(declared.group(1)) == len(names), (
        f"docs/02 §4.3 は {declared.group(1)} 項目と記載しているが "
        f"WEIGHT_WIDGETS は {len(names)} 項目"
    )

    # 表にも全項目が載っていること
    for name in names:
        assert f"`{name}`" in section, f"docs/02 §4.3 の表に {name} が無い"


def test_公平性の既定重みがドキュメント内で矛盾しない() -> None:
    """``docs/08`` 内で公平性の既定重みが矛盾していないこと。

    修正前: 同 §3.2 は「既定 0.0 = 無効」、§8 は「既定重み 4.0（有効）」と
    書いていた。``domain.ObjectiveWeights`` の実体は 4.0 なので §3.2 が古かった。
    """
    import dataclasses
    import sys

    sys.path.insert(0, str(SRC))
    try:
        from shiftai.domain import ObjectiveWeights
    finally:
        sys.path.remove(str(SRC))

    plan = _doc_text(PLAN08)
    defaults = {
        f.name: f.default
        for f in dataclasses.fields(ObjectiveWeights)
        if f.name.startswith("fairness_")
    }
    assert defaults, "ObjectiveWeights に fairness_* が無い"

    # 各 fairness_*.py: 定義として現れた既定値を集める
    declared = dict(re.findall(r"(fairness_\w+_penalty)\s*:\s*float\s*=\s*([\d.]+)", plan))
    assert declared, "docs/08 に fairness_* の既定値定義がない"

    for name, expected in defaults.items():
        assert name in declared, f"docs/08 に {name} の定義がない"
        got = float(declared[name])
        assert got == float(expected), f"docs/08 の {name} 既定 {got} が実装の {expected} と不一致"


def test_requirements_txtとpyprojectの依存が一致する() -> None:
    """``requirements.txt`` が ``pyproject.toml`` の実行時依存と一致すること。

    修正前: 運用手順が両方dipendant なindrome として存在するのに、
    検証環境の記載だけが古く（Python 3.12.3 / streamlit 1.63.0 / pandas 3.0.5 /
    numpy 2.4.6）、実際に動く環境（3.13 / 1.64 / 3.0.6 / 2.5.3）と食い違っていた。
    乖離がmeasuredculate できる形にしておく。
    """
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        re.split(r"[<>=!\[ ]", spec, maxsplit=1)[0].strip().lower()
        for spec in data["project"]["dependencies"]
    }
    assert declared, "pyproject.toml に実行時依存が無い"

    req_text = _doc_text(REQUIREMENTS)
    pinned = set()
    for line in req_text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        for spec in line.split(","):
            name = re.split(r"[<>=!\[ ]", spec.strip(), maxsplit=1)[0].strip().lower()
            if name:
                pinned.add(name)
    assert pinned, "requirements.txt に依存が書かれていない"

    assert pinned == declared, (
        "requirements.txt と pyproject.toml の依存が不一致:\n"
        f"  requirements.txt のみ: {sorted(pinned - declared)}\n"
        f"  pyproject.toml のみ : {sorted(declared - pinned)}"
    )

    # 開発用依存が混ざっていないこと（混ざると本番打入に pytest 等が入る）
    dev = set(
        re.split(r"[<>=!\[ ]", spec, maxsplit=1)[0].strip().lower()
        for spec in data["project"]["optional-dependencies"]["dev"]
    )
    assert not (pinned & dev), (
        f"requirements.txt に開発用依存が混ざっている: {sorted(pinned & dev)}"
    )


def _verified_env_versions(text: str) -> set[str]:
    """「検証環境の実測」の版数行だけを、装飾を除いて集める。

    判定条件は「streamlit / pandas / numpy が同時に載り、3 桁数の版数がある行」。
    これにより依存の**制約**（``pandas>=3.0,<4``）や
    ``streamlit>=1.63`` のような下限 pin を版数記載と取り違えない。
    """
    return {
        " ".join(line.replace("`", "").replace("#", " ").replace("/", " ").split())
        for line in text.splitlines()
        if "streamlit" in line
        and "pandas" in line
        and "numpy" in line
        and re.search(r"\d+\.\d+\.\d+", line)
    }


def _verified_env_date_and_python(text: str) -> tuple[str, str]:
    """「検証環境の実測」行から (日付, Python の X.Y) を取り出す。

    全角括弧などを正規表現に載せない（``re`` の ``\\uffxx`` エスケープ解釈が
    全角文字の境界で不安定なため）。ASCII だけで取り出す。
    """
    date_m = re.search(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text)
    assert date_m, "requirements.txt に検証環境の日付が無い"
    py_m = re.search(r"/ Python ([0-9]+\.[0-9]+)", text)
    assert py_m, "requirements.txt に検証環境の Python 版が無い"
    return date_m.group(0), py_m.group(1)


def test_検証環境の記載が陳腐化していない() -> None:
    """``requirements.txt`` の「検証環境の実測」がwell-formed であること。

    日付（YYYY-MM-DD）・Python 版を持ち、未来の日和对 ``requires-python``
    の下限を満たすこと。3 つ揃っていないと、検証環境がいつ・どれだったか
    追えなくなる。

    修正前: 記載はあったが 2026-09-28 / Python 3.12.3 のままで、
    実際に動く 3.13 環境と食い違っていた。
    """
    date_str, py_ver = _verified_env_date_and_python(_doc_text(REQUIREMENTS))

    import datetime as _dt

    parsed = _dt.date.fromisoformat(date_str)
    assert parsed <= _dt.date.today(), f"requirements.txt の検証環境の実測が {date_str}（未来）"

    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = data["project"]["requires-python"]
    min_minor = int(re.search(r"3\.(\d+)", requires).group(1))
    assert int(py_ver.split(".")[1]) >= min_minor, (
        f"requirements.txt の検証環境 Python {py_ver} が requires-python {requires} に満たない"
    )


def test_検証環境の記載がドキュメント間で一致する() -> None:
    """「検証環境の実測」の版数が ``requirements.txt`` を正として揃っていること。

    修正前: ``requirements.txt`` / ``docs/02`` / ``docs/05`` が
    「2026-09-28 / Python 3.12.3: streamlit 1.63.0 / pandas 3.0.5 / numpy 2.4.6」
    を 3 箇所に書いていたが、実際の検証環境（3.13 / 1.64 / 3.0.6 / 2.5.3）と
    食い違っていた。日付・Python 版も 3 箇所でバラバラだった。

    ``requirements.txt`` の「検証環境の実測」を**唯一の正**とし、
    他のファイル（``requirements-dev.txt`` / ``docs/02`` / ``docs/05``）は
    同じ日付・同じ Python 版を書くこと。
    """
    req = _doc_text(REQUIREMENTS)
    date_str, py_ver = _verified_env_date_and_python(req)

    canonical = _verified_env_versions(req)
    assert len(canonical) == 1, (
        "requirements.txt の『検証環境の実測』の版数記載が割れている: "
        f"{sorted(canonical)}（streamlit / pandas / numpy / PuLP / openpyxl が並ぶ行は 1 行）"
    )
    expected_line = next(iter(canonical))

    for name, text in (
        ("docs/02_ソルバ仕様.md", _doc_text(SOLVER_SPEC)),
        ("docs/05_開発ガイド.md", _doc_text(GUIDE)),
        ("requirements-dev.txt", _doc_text(REQUIREMENTS_DEV)),
    ):
        found = _verified_env_versions(text)
        if name == "requirements-dev.txt":
            # 開発用依存だけ並ぶ行なので、版数一致の判定は日付・Python 版に留める
            assert date_str in text, f"{name} に検証環境の日付 {date_str} の記載が無い"
            assert py_ver in set(re.findall(r"Python ([0-9]+\.[0-9]+)", text)), (
                f"{name} に Python {py_ver} の記載が無い"
            )
            continue
        assert expected_line in found, (
            f"{name} の検証環境の版数が requirements.txt と一致しない:\n"
            f"  期待: {expected_line}\n"
            f"  実際: {sorted(found) or '(記載なし)'}"
        )
        assert date_str in text, f"{name} に検証環境の日付 {date_str} の記載が無い"
        assert py_ver in set(re.findall(r"Python ([0-9]+\.[0-9]+)", text)), (
            f"{name} に Python {py_ver} の記載が無い"
        )


# ---------------------------------------------------------------------------
# README: リンク・目次・デモが壊れていないこと
# ---------------------------------------------------------------------------

#: **コードポイントで見分けられる**外国語スクリプト。
#:
#: 簡体字中国語は日本語の漢字と同じコードポイント帯（U+4E00-9FFF）に在るため
#: この方法では検出できない。簡体字と韓国語は
#: ``tests/test_static_guards.py::test_簡体字が混入していない`` が
#: ``SIMPLIFIED_ONLY`` の一覧で担当している（README も検査対象）。
#: ここは「一覧に載っていない言語」のみを引き受ける。
_FOREIGN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0400, 0x04FF),  # Cyrillic
    (0x0370, 0x03FF),  # Greek
    (0x1F00, 0x1FFF),  # Greek Extended
    (0xAC00, 0xD7AF),  # Hangul
    (0x1100, 0x11FF),  # Hangul Jamo
    (0x3130, 0x318F),  # Hangul Compatibility Jamo
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic
    (0x0750, 0x077F),  # Arabic Supplement
    (0x0E00, 0x0E7F),  # Thai
    (0x0900, 0x097F),  # Devanagari
)


def test_READMEのdocsリンクが実在する() -> None:
    """README が ``[...](docs/xx.md)`` で参照するファイルが全て存在すること。

    ファイル名を間違えるとリンク切れになる。
    """
    text = _doc_text(README)
    missing = [
        target
        for target in re.findall(r"\]\((docs/[^)#]+)\)", text)
        if not (ROOT / target).exists()
    ]
    assert not missing, f"README が参照しているファイルが無い: {sorted(set(missing))}"


def test_READMEの目次アンカーが見出しに対応する() -> None:
    """``[...](#anchor)`` のすべてが実際の見出しに解決されること。

    GitHub の slug 規則（記号は除去し、空白を ``-`` にする）に合わせている。
    """
    text = _doc_text(README)

    def slug(heading: str) -> str:
        lowered = heading.strip().lower()
        kept = "".join(c for c in lowered if c.isalnum() or c in " -_")
        return kept.replace(" ", "-")

    headings = {
        slug(line.lstrip("#").strip()) for line in text.splitlines() if line.startswith("#")
    }
    assert headings, "README に見出しが無い"

    dangling = sorted({a for a in re.findall(r"\]\(#([^)]+)\)", text) if a not in headings})
    assert not dangling, f"対応する見出しが無いアンカー: {dangling}"


def test_READMEに外国語スクリプトが混ざっていない() -> None:
    """README の本文に、コードポイントで見分けられる外国語スクリプトが無いこと。

    誤変換や機械翻訳の混入は結合が正しくなった日本語として読めてしまうため、
    記号付きの見た目による目視では気づきにくい。検出したら本文を直す。
    （簡体字中国語と韓国語は ``test_簡体字が混入していない`` が担当する）
    """
    offenders: list[str] = []
    for lineno, line in enumerate(_doc_text(README).splitlines(), 1):
        for char in line:
            code = ord(char)
            if any(lo <= code <= hi for lo, hi in _FOREIGN_RANGES):
                offenders.append(f"README.md:{lineno}: {char!r} | {line.strip()[:60]}")
                break
    assert not offenders, "外国語スクリプトが混入しています:\n" + "\n".join(offenders)


def test_READMEのPythonデモが実際に動く() -> None:
    """README に載せた Python デモが、そのまま実行できること。

    デモが壊れていたら読者が最初に触る部分なので、
    **記載したコードを実際に走らせる**ことで保証する。
    最適解が求まる規模なので CBC が起動する（``slow`` 自動付与の対象）。
    """
    import sys
    import textwrap

    from tests._utf8_subprocess import run_utf8

    text = _doc_text(README)
    blocks = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    assert blocks, "README に Python コードブロックが無い"

    code = textwrap.dedent(blocks[0])
    # ``python -c`` は CLI unlike ``python -m shiftai`` で _force_utf8_streams を
    # 経由しないため、Windows の日本語環境では出力が cp932 になる。
    # 共通ヘルパで PYTHONIOENCODING=utf-8 を明示し、ロケールに依存させない。
    result = run_utf8([sys.executable, "-c", code], cwd=ROOT)
    assert result.returncode == 0, (
        "README の Python デモが失敗しました:\n"
        f"stdout:\n{result.stdout[-1500:]}\nstderr:\n{result.stderr[-1500:]}"
    )
    # デモが「解けている」ことと「検査が 0 件」であることを確認する
    assert "最適解" in result.stdout, f"デモが最適解を返していません:\n{result.stdout[-1500:]}"
    assert "BLOCKER 0 件 / 全体 0 件" in result.stdout, (
        f"デモの検査結果が変わっています:\n{result.stdout[-1500:]}"
    )
    # README が掲載している出力と食い違っていないこと（抜粋した部分で一致）
    for marker in (
        "必要人員: 12 時間帯",
        "定員比8:1、切り上げ",
        "S01 井上: 5.0 時間（300 分）",
    ):
        assert marker in text, f"README の出力例に {marker!r} が無い"
        assert marker in result.stdout, (
            f"README は {marker!r} と記載しているが実際の出力が違う:\n{result.stdout[-1500:]}"
        )


def test_開発ガイドがslow手動付与を指示していない() -> None:
    """docs/05 が ``@pytest.mark.slow`` の手動付与を指示していないこと。

    現在は ``tests/conftest.py`` が CBC 起動テストに **自動付与** する。

    修正前: ``"slow を付ける" in guide or "slow を付ける" in guide`` は
    **左右が同一文字列**で完全な no-op であり、
    ``assert "自動" in guide`` もファイル全体をgrepするため
    「完全に通らない」テストになっていた（本テスト自身が空回り）。
    ここでは「手動付与を指示する文」が**現れているなら**、
    同じ文の近くに「自動」の記載があるかを**行単位で**確認する。
    """
    guide = _doc_text(GUIDE)
    offenders: list[str] = []
    for lineno, line in enumerate(guide.splitlines(), 1):
        if "slow" not in line and "slow" not in line.lower():
            continue
        # 「付ける」「付けて」+ 手動付与を指示している文
        if not re.search(r"slow[^。\n]{0,12}(を付|を付し)", line):
            continue
        if re.search(r"自動", line):
            continue
        context = "\n".join(guide.splitlines()[max(0, lineno - 4) : lineno + 1])
        if "自動" in context:
            continue
        offenders.append(f"docs/05:{lineno}: {line.strip()[:70]}")
    assert not offenders, (
        "docs/05 が slow の手動付与を指示している（現在は自動付与）:\n" + "\n".join(offenders)
    )


def test_開発ガイドがslowの自動付与を説明している() -> None:
    """docs/05 が ``slow`` の自動付与を説明していること。

    実際に存在しない仕組み（手動付与）を指示しないために、
    自動付与の説明があることを確認する。
    """
    guide = _doc_text(GUIDE)
    assert "slow" in guide, "docs/05 に slow の説明がない"
    assert re.search(r"slow[^。\n]{0,40}自動|自動[^。\n]{0,40}slow", guide), (
        "docs/05 に slow の自動付与の説明がない"
    )
