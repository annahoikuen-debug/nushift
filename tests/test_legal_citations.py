"""法令引用の静的ガード（docs/12 Step 1-3）。

誤った条文番号が仕様としてコードやドキュメントに根付くのを防ぐ。誤りは実際に
発生していた（`docs/12_実装計画_法令是正化_リグレッション防止.md`）。

- 労働基準法第9条は「労働者」の**定義**条項であり、労働時間・休憩・休息の根拠にならない
  - 労働時間 → 第32条
  - 休憩 → 第34条
  - 勤務間休息11時間 → 労働安全衛生法第68条第2項
- 第32条の3は「始業・終業時刻の決定を委ねる労働者」で、休息日ではない（休日は第35条）

``docs/11`` / ``docs/12`` は誤った引用を「誤りの例」として引用しているため、
対象から除外する。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "shiftai"
DOCS = ROOT / "docs"

#: 誤った引用を「誤りの例」として記載している実装計画は走査対象外。
_EXEMPT = frozenset({"11_", "12_"})

#: (正規表現, 誤りの説明)
FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"第9条[^\n]{0,20}(労働時間|休憩|休息)"),
        "労働基準法第9条は定義条項。労働時間は第32条、休憩は第34条、"
        "勤務間休息は労働安全衛生法第68条第2項",
    ),
    (
        re.compile(r"(労働時間|休憩|休息)[^\n]{0,20}第9条"),
        "労働基準法第9条は定義条項。労働時間は第32条、休憩は第34条、"
        "勤務間休息は労働安全衛生法第68条第2項",
    ),
    (
        re.compile(r"第32条の3[^\n]{0,10}(休息日|休日)"),
        "第32条の3はフレックス・清算期間型。休日は第35条",
    ),
    (
        re.compile(r"(休息日|休日)[^\n]{0,10}第32条の3"),
        "第32条の3はフレックス・清算期間型。休日は第35条",
    ),
)


def _targets() -> list[Path]:
    files = [p for p in SRC.rglob("*.py")]
    files += [p for p in DOCS.glob("*.md") if not p.name.startswith(tuple(_EXEMPT))]
    files += [ROOT / "README.md", ROOT / "gas" / "app.js"]
    return [p for p in files if p.is_file()]


@pytest.mark.parametrize("pattern,reason", FORBIDDEN, ids=[r.pattern for r, _ in FORBIDDEN])
def test_obsolete_legal_citations_absent(pattern: re.Pattern[str], reason: str) -> None:
    """廃止された条文引用がソース・ドキュメントに混入していないこと。"""
    offenders: list[str] = []
    for path in _targets():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{lineno}: {line.strip()}")
    assert not offenders, (
        f"誤った法令引用を検出（{reason}）:\n" + "\n".join(sorted(offenders))
    )


def test_休憩と休息の根拠条項が明記されている() -> None:
    """主要な根拠が正しい条番号でソースに残っていること（是正の取りこぼし防止）。"""
    domain_source = (SRC / "domain.py").read_text(encoding="utf-8")
    assert "労働安全衛生法第68条第2項" in domain_source, "勤務間休息の根拠条文が失われている"
    assert "労働基準法第34条" in domain_source, "休憩の根拠条文が失われている"


def test_週44時間と月45時間は社内目安である() -> None:
    """旧基準の定数を法定と断定しないこと（緩める方向の変更を禁じるガード）。"""
    source = (SRC / "config.py").read_text(encoding="utf-8")
    for name in ("STATUTORY_WEEKLY_WORK_HOURS", "STATUTORY_OVERTIME_LIMIT_HOURS"):
        doc = source.split(f"{name} =", 1)[1]
        assert "社内目安" in doc.split('"""', 2)[1], f"{name} に社内目安である旨が無い"