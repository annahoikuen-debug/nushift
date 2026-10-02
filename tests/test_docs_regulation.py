"""制度切替（オプション F〜E）で追加したドキュメント記述が実装と一致することの検証。

**なぜこのテストが要るか**

監査で判明した最大のリスクは「**未検証の仮定値が制度の解釈として流通する**」ことだった。
`local_rules.py` の札幌のように「代替措置を『なし』と仮定（要確認）」という値が
プリセットとして正規に並んでいる。制度を追加したaryn也只是、
免責（``SCOPE_STATEMENT``）と法令番号と算手法が実装と食い違えば
そのまま現場に流通する。ここではその3点を固定する。
"""

from __future__ import annotations

import re

import pytest

from shiftai import local_rules
from shiftai.compliance import FacilitySpec, Regulation, StaffRecord, audit_facility
from shiftai.domain import AgeClass, Role

DOC = "docs/03_配置基準と自治体ルール.md"
GUIDE = "docs/05_開発ガイド.md"
SOURCE = "src/shiftai/local_rules.py"

LICENSED_KEYS = (
    "全国基準（厚労省）",
    "東京都",
    "横浜市",
    "大阪市",
    "福岡市",
    "名古屋市",
    "京都市",
    "札幌市",
    "神戸市",
    "川崎市",
    "保育標準時間のみ園",
)
UNLICENSED_KEYS = (
    "認可外保育施設（指導監督基準）",
    "企業主導型保育事業（単独枠）",
    "企業主導型保育事業（保育事業者型・20名以上）",
)


def read(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 免責が実装に載っていること
# ---------------------------------------------------------------------------


def test_適用範囲が明示的に宣言されている() -> None:
    statement = local_rules.SCOPE_STATEMENT
    assert "認可保育所" in statement
    assert "認可外保育施設" in statement
    assert "流用できない" in statement


def test_適用範囲が説明カードに出る() -> None:
    for key in LICENSED_KEYS:
        cards = {note.key for note in local_rules.local_rule_notes(local_rules.get_standard(key))}
        assert "scope" in cards, key
        detail = next(
            note.detail
            for note in local_rules.local_rule_notes(local_rules.get_standard(key))
            if note.key == "scope"
        )
        assert "認可外保育施設" in detail, key
        assert "最大 4 名" in detail, key


def test_ソースのdocstringも適用範囲を明記している() -> None:
    source = read(SOURCE)
    docstring = source[: source.index('"""', 10)]
    assert "認可外保育施設" in docstring
    assert "headcount_mode" in docstring


def test_未対応の制度が明記されている() -> None:
    doc = read(DOC)
    assert "小規模保育事業" in doc
    assert "事業所内保育事業" in doc
    assert "未対応" in doc or "プリセットを用意していない" in doc


# ---------------------------------------------------------------------------
# 2. プリセット一覧の記載が実装と一致すること
# ---------------------------------------------------------------------------


def test_プリセット数が記載と一致する() -> None:
    doc = read(DOC)
    match = re.search(r"\*\*プリセット数:\s*(\d+)\s*種類\*\*", doc)
    assert match, "docs/03 にプリセット数の記載がない"
    assert int(match.group(1)) == len(local_rules.MUNICIPAL_PRESETS)
    assert len(local_rules.MUNICIPAL_PRESETS) == len(LICENSED_KEYS) + len(UNLICENSED_KEYS)


@pytest.mark.parametrize("key", LICENSED_KEYS + UNLICENSED_KEYS)
def test_すべてのプリセットがdocsの表に載っている(key: str) -> None:
    doc = read(DOC)
    assert f"| {key} |" in doc, key


@pytest.mark.parametrize("key", UNLICENSED_KEYS)
def test_認可外のプリセットは要綱の条項番号を書く(key: str) -> None:
    """条項番号が無いと「どの基準のどの部分かが」現場で確認できない。"""
    standard = local_rules.get_standard(key)
    assert "第3の2(4)" in standard.remarks or "第1" in standard.remarks, key
    source = local_rules.preset_source(standard)
    assert "要綱" in source or "指導監督基準" in source, key


def test_算手法の表が実装と一致する() -> None:
    doc = read(DOC)
    for key in UNLICENSED_KEYS:
        standard = local_rules.get_standard(key)
        assert standard.headcount_mode == "facility_formula", key
        assert standard.headcount_extra == 1, key
    assert "facility_formula" in doc
    assert "`per_class`" in doc


def test_看護師の扱いの違いが文書化されている() -> None:
    doc = read(DOC)
    assert "みなし保育士" in doc
    assert "qualified_extra_roles" in doc
    assert "nurse_as_qualified_cap" in doc


# ---------------------------------------------------------------------------
# 3. 既知の限界の記載が実態と一致すること
# ---------------------------------------------------------------------------


def test_未使用フィールドの記載が実態と一致する() -> None:
    """``min_qualified_ratio`` は ratio モードでのみ効く、という限定付きに直す。"""
    doc = read(DOC)
    assert "min_qualified_ratio" in doc
    assert "qualified_mode" in doc
    # 「使われていない」という古い主張が、限定なしに残っていないこと
    for line in doc.splitlines():
        if "min_qualified_ratio" not in line or "使われていない" not in line:
            continue
        assert "per_class" in line or "限定" in line, f"古い記述が残っています: {line.strip()}"


def test_未対応制度の記入が実装と矛盾しない() -> None:
    """小規模保育事業・事業所内保育事業にプリセットが無いこと。"""
    for name in ("小規模保育事業", "事業所内保育事業"):
        assert Regulation(name).preset_key is None
        assert name not in local_rules.MUNICIPAL_PRESETS
        with pytest.raises(ValueError):
            audit_facility(FacilitySpec(regulation=Regulation(name)))


# ---------------------------------------------------------------------------
# 4. 10 章（制度適合チェック）が実装と一致すること
# ---------------------------------------------------------------------------


def test_判定項目が文書と一致する() -> None:
    doc = read(DOC)
    report = audit_facility(
        FacilitySpec(
            regulation=Regulation.CORPORATE_LED,
            capacity=40,
            monthly_children={AgeClass.INFANT: 5},
            staff=(
                StaffRecord("H1", "A", (Role.HOIKUSHI,), 40.0),
                StaffRecord("K1", "B", (Role.CHUUBOU,), 40.0),
            ),
            has_contract_doctor=True,
            cooking_outsourced=False,
        )
    )
    for check in report.checks:
        assert f"`{check.key}`" in doc, check.key


def test_判定項目数が12であること() -> None:
    report = audit_facility(
        FacilitySpec(
            regulation=Regulation.CORPORATE_LED,
            capacity=40,
            monthly_children={AgeClass.INFANT: 5},
            staff=(StaffRecord("H1", "A", (Role.HOIKUSHI,), 40.0),),
        )
    )
    assert len(report.checks) == 12


def test_CLIの終了コード契約が文書と一致する() -> None:
    guide = read(GUIDE)
    assert "0（報告可）／3（不適合または未確認あり）／1（入力エラー）" in guide
    doc = read(DOC)
    assert "shiftai compliance" in doc
    assert "`--support-certified`" in doc


def test_報表不可の扱いが文書化されている() -> None:
    doc = read(DOC)
    assert "is_filing_ready" in doc
    assert "未確認" in doc
    assert "報告に使える状態ではない" in doc
