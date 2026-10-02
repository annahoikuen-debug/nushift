"""``shiftai compliance`` サブコマンドの検証。

**なぜこのテストが要るか**

認可外保育施設の届出・月次報告・巡回指導では、
シフトが是否符合ではなく**1 日の常勤換算人数と施設の属性**で判定する。
この入出力を CLI から使えるようにしないと、チェックリストは
コードの中只能用になり現場では使えない。
終了コードの契約（不適合・未確認があれば 3）も合わせて固定する。
"""

from __future__ import annotations

import json

import pytest

from shiftai.__main__ import EXIT_ERROR, EXIT_OK, EXIT_WARNING, main
from shiftai.domain import AgeClass


@pytest.fixture
def bundle(tmp_path):
    """園児・職員の CSV を作る。"""
    from shiftai import sample_data

    return sample_data.write_sample_files(tmp_path / "in", days=None, seed=1)


def _run(args: list[str]) -> int:
    return main(args)


def test_complianceはMarkdownで判定を出す(bundle, capsys) -> None:
    code = _run(
        [
            "compliance",
            "--children",
            str(bundle["children"]),
            "--staff",
            str(bundle["staff"]),
            "--regulation",
            "認可外保育施設",
            "--capacity",
            "40",
            "--facility",
            "テスト園",
            "--support-certified",
        ]
    )
    out = capsys.readouterr().out
    assert "適合チェック（テスト園）" in out
    assert "必要保育従事者数" in out
    assert "算出手法と制度の整合" in out
    # 未確認（面積・嘱託医・調理）があるので報告不可＝終了コード 3
    assert code == EXIT_WARNING


def test_判定結果をCSVで出力できる(bundle, tmp_path, capsys) -> None:
    out_path = tmp_path / "check.csv"
    code = _run(
        [
            "compliance",
            "--children",
            str(bundle["children"]),
            "--staff",
            str(bundle["staff"]),
            "--regulation",
            "企業主導型保育事業",
            "--capacity",
            "40",
            "--support-certified",
            "--out",
            str(out_path),
        ]
    )
    assert code in (EXIT_OK, EXIT_WARNING)
    assert out_path.is_file()
    text = out_path.read_text(encoding="utf-8")
    assert "必要保育従事者数" in text


def test_JSON出力は件数と判定を返す(bundle, capsys) -> None:
    _run(
        [
            "compliance",
            "--children",
            str(bundle["children"]),
            "--staff",
            str(bundle["staff"]),
            "--regulation",
            "企業主導型保育事業",
            "--capacity",
            "40",
            "--support-certified",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["制度"] == "企業主導型保育事業"
    assert isinstance(payload["不適合"], int)
    assert isinstance(payload["未確認"], int)
    assert payload["報告可"] is False
    assert len(payload["チェック"]) == len(payload["チェック"])


def test_制度名が不正なら終了コード1(capsys) -> None:
    assert _run(["compliance", "--regulation", "存在しない制度"]) == EXIT_ERROR
    assert "制度名が不正" in capsys.readouterr().err


def test_プリセットが無い制度は案内して終了コード3(tmp_path, capsys) -> None:
    """小規模保育事業は A/B/C 型で別基準なので自動選択できない。"""
    from shiftai import sample_data

    files = sample_data.write_sample_files(tmp_path / "in", days=None, seed=1)
    code = _run(
        [
            "compliance",
            "--children",
            str(files["children"]),
            "--staff",
            str(files["staff"]),
            "--regulation",
            "小規模保育事業",
        ]
    )
    assert code in (EXIT_WARNING, EXIT_ERROR)
    captured = capsys.readouterr()
    assert "プリセット" in captured.out + captured.err


def test_初期状態でも落ちない(capsys) -> None:
    """園児・職員を指定しなくても Python 例外にならないこと。"""
    code = _run(["compliance", "--regulation", "認可保育所", "--capacity", "60"])
    assert code in (EXIT_OK, EXIT_WARNING)


def test_在園児ゼロは該当なしになる(tmp_path, capsys) -> None:
    from shiftai import sample_data

    files = sample_data.write_sample_files(tmp_path / "in", days=None, seed=1)
    code = _run(
        [
            "compliance",
            "--children",
            str(files["children"]),
            "--staff",
            str(files["staff"]),
            "--regulation",
            "企業主導型保育事業",
            "--capacity",
            "0",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    titles = {row["項目"] for row in payload["チェック"]}
    assert "必要保育従事者数（常勤換算）" in titles
    assert "最低配置人数" in titles
    assert code in (EXIT_OK, EXIT_WARNING)
    assert AgeClass is not None
