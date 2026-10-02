"""``shiftai solve`` の終了コードとオプションの回帰テスト（T-08）。

終了コードは運用上の約束なので、直接検証する。

======  ==========================================  ==============
条件     内容                                        終了コード
======  ==========================================  ==============
解なし   ``result.ok`` が False                     1
法令違反  BLOCKER が 1 件以上（``--strict`` の有無に関わらず）    2
要調整のみ ``--strict`` ありかつ WARNING が 1 件以上   3
それ以外 —                                          0
======  ==========================================  ==============

``INFO`` は「参考」の情報なので ``--strict`` を付けても 3 にしない。

終了コードの判定は :func:`shiftai.__main__.resolve_exit_code` に切り出してある。
実データで WARNING だけの結果を作るのは難しいため、判定部分は合成した
違反一覧で直接検証し、CLI 経由は終了コードと入出力の契約だけを確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shiftai.__main__ import (
    EXIT_BLOCKER,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_WARNING,
    main,
    resolve_exit_code,
)
from shiftai.domain import Violation, ViolationSeverity

SLOW = pytest.mark.slow


class _FakeResult:
    """``ok`` プロパティだけを持つ結果の代用。"""

    def __init__(self, ok: bool) -> None:
        self.ok = ok


def violation(severity: ViolationSeverity, code: str = "X") -> Violation:
    return Violation(severity=severity, code=code, message=f"{code} のテスト用")


# --- 終了コードの判定（R1〜R5） --------------------------------------------


def test_解なしは終了コード1() -> None:
    assert resolve_exit_code(_FakeResult(False), [], strict=False) == EXIT_ERROR
    assert resolve_exit_code(_FakeResult(False), [], strict=True) == EXIT_ERROR


def test_解ありかつ違反なしは0() -> None:
    assert resolve_exit_code(_FakeResult(True), [], strict=False) == EXIT_OK
    assert resolve_exit_code(_FakeResult(True), [], strict=True) == EXIT_OK


def test_BLOCKERがあればstrict有無に関わらず2() -> None:
    violations = [violation(ViolationSeverity.BLOCKER, "SHORTFALL_STAFF")]
    assert resolve_exit_code(_FakeResult(True), violations, strict=False) == EXIT_BLOCKER
    assert resolve_exit_code(_FakeResult(True), violations, strict=True) == EXIT_BLOCKER


def test_strictなしでWARNINGがあっても0() -> None:
    """WARNING は「要調整」であり、``--strict`` 無しでは失敗扱いにならないこと。"""
    violations = [violation(ViolationSeverity.WARNING, "SPLIT_SHIFT")]
    assert resolve_exit_code(_FakeResult(True), violations, strict=False) == EXIT_OK


def test_strictありでWARNINGがあれば3() -> None:
    violations = [violation(ViolationSeverity.WARNING, "SPLIT_SHIFT")]
    assert resolve_exit_code(_FakeResult(True), violations, strict=True) == EXIT_WARNING


def test_strictありでもINFOのみなら0() -> None:
    """INFO（参考）は判定に含めないこと。``--strict`` を付けても 3 にしない。"""
    violations = [
        violation(ViolationSeverity.INFO, "HOURS_IMBALANCE"),
        violation(ViolationSeverity.INFO, "BREAK_OVERLAP"),
    ]
    assert resolve_exit_code(_FakeResult(True), violations, strict=True) == EXIT_OK


def test_BLOCKERとWARNINGが混在しても2が優先される() -> None:
    violations = [
        violation(ViolationSeverity.WARNING, "SPLIT_SHIFT"),
        violation(ViolationSeverity.BLOCKER, "SHORTFALL_STAFF"),
    ]
    assert resolve_exit_code(_FakeResult(True), violations, strict=True) == EXIT_BLOCKER


def test_退出コードの定数が仕様どおりである() -> None:
    assert (EXIT_OK, EXIT_ERROR, EXIT_BLOCKER, EXIT_WARNING) == (0, 1, 2, 3)


# --- CLI 経由の契約（R5〜R11） ----------------------------------------------


def _sample_dir(tmp_path: Path) -> Path:
    """``shiftai sample`` で入力を書き出す。"""
    out = tmp_path / "sample"
    assert main(["sample", "--out", str(out), "--days", "1"]) == EXIT_OK
    return out


def test_入力不備は終了コード1でTracebackを出さない(tmp_path, capsys) -> None:
    """壊れた CSV は 1 で終わり、内部のトレースを画面に出さないこと。"""
    sample = _sample_dir(tmp_path)
    broken = sample / "broken.csv"
    lines = (sample / "children.csv").read_text(encoding="utf-8-sig").splitlines()
    lines[1] = lines[1].replace("2026-", "202X-", 1)
    broken.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    code = main(
        [
            "solve",
            "--children",
            str(broken),
            "--staff",
            str(sample / "staff.csv"),
            "--out",
            str(tmp_path / "out"),
            "--time-limit",
            "5",
        ]
    )
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    # Python が例外を整形して出力するときの正式な Traceback 表記だけを NG とする
    assert "Traceback (most recent call last)" not in captured.out + captured.err


def test_versionオプションが値を返す(capsys) -> None:
    """``--version`` は argparse の ``action="version"`` なので SystemExit(0) する。"""
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "shiftai" in out
    assert any(ch.isdigit() for ch in out), f"バージョンが表示されない: {out!r}"


def test_presetsの項目が日本語ラベルを持つ(capsys) -> None:
    assert main(["presets", "--json"]) == EXIT_OK
    items = json.loads(capsys.readouterr().out)
    assert items
    assert all(item["key"] and item["summary"] for item in items)


# --- 入出力オプションの検証（R6〜R11、slow） --------------------------------


@SLOW
def test_closed指定日が要員表から除外される(tmp_path) -> None:
    """``--closed`` で指定した日は必要人員表に現れないこと。"""
    import pandas as pd

    sample = _sample_dir(tmp_path)
    out = tmp_path / "out"
    children = pd.read_csv(sample / "children.csv")
    target = str(children["登園日"].iloc[0])

    code = main(
        [
            "solve",
            "--children",
            str(sample / "children.csv"),
            "--staff",
            str(sample / "staff.csv"),
            "--closed",
            target,
            "--out",
            str(out),
            "--time-limit",
            "20",
        ]
    )
    assert code in (EXIT_OK, EXIT_BLOCKER)
    requirements = pd.read_csv(out / "requirements.csv")
    assert target not in set(requirements["日付"].astype(str))


def _one_child_dir(tmp_path: Path) -> Path:
    """1 名の高年齢児だけの入力を書き出す。

    高年齢の定員比は 20:1 なので、必要人員は自然に 1 名になる。
    2名ルールが働くか確かめるには「1 名しか要らない時間帯」を作る必要がある。
    """
    day = tmp_path / "one"
    day.mkdir()
    (day / "children.csv").write_text(
        "園児ID,氏名,年齢,登園日,登園時刻,降園時刻,短時間保育,欠席,欠席理由,"
        "早朝保育,延長保育,備考\n"
        "C001,ひとり,5,2026-09-28,09:00,15:00,false,false,,false,false,\n",
        encoding="utf-8-sig",
    )
    assert main(["sample", "--out", str(tmp_path / "staff_out"), "--days", "1"]) == EXIT_OK
    staff = (tmp_path / "staff_out" / "staff.csv").read_text(encoding="utf-8-sig")
    (day / "staff.csv").write_text(staff, encoding="utf-8-sig")
    return day


def test_no_min_twoで2名ルールの底上げが外れる(tmp_path) -> None:
    """``--no-min-two`` で「1 名しか要らない時間帯」が底上げされないこと。

    高年齢児 1 名だけの園では定員比 20:1 で必要人員は 1 名。2名ルールが
    人数を 2 名に底上げするが、``--no-min-two`` を付けると 1 名のままになる。
    根拠列に「2名ルールによる底上げ」が現れるかどうかで判定する。
    """
    import pandas as pd

    day = _one_child_dir(tmp_path)

    def build(out_name: str, extra: list[str]) -> list[str]:
        return [
            "solve",
            "--children",
            str(day / "children.csv"),
            "--staff",
            str(day / "staff.csv"),
            "--start",
            "2026-09-28",
            "--end",
            "2026-09-28",
            "--out",
            str(tmp_path / out_name),
            "--time-limit",
            "10",
            *extra,
        ]

    assert main(build("out", [])) in (EXIT_OK, EXIT_BLOCKER)
    with_min_two = pd.read_csv(tmp_path / "out" / "requirements.csv")

    assert main(build("out2", ["--no-min-two"])) in (EXIT_OK, EXIT_BLOCKER)
    without = pd.read_csv(tmp_path / "out2" / "requirements.csv")

    assert "2名ルール" in "\n".join(with_min_two["根拠"].astype(str)), (
        "既定では底上げが起こるること"
    )
    assert "2名ルール" not in "\n".join(without["根拠"].astype(str)), "底上げが起こらないこと"
    assert without["必要人員"].max() <= with_min_two["必要人員"].max()


@SLOW
def test_facility指定がsummaryに出る(tmp_path) -> None:
    """``--facility`` の名前が ``summary.md`` に反映されること。"""
    sample = _sample_dir(tmp_path)
    out = tmp_path / "out"
    code = main(
        [
            "solve",
            "--children",
            str(sample / "children.csv"),
            "--staff",
            str(sample / "staff.csv"),
            "--facility",
            "あさひ幼稚園",
            "--out",
            str(out),
            "--time-limit",
            "20",
        ]
    )
    assert code in (EXIT_OK, EXIT_BLOCKER)
    assert "あさひ幼稚園" in (out / "summary.md").read_text(encoding="utf-8")


@SLOW
def test_zip指定でbundle的中身が読める(tmp_path) -> None:
    """``--zip`` で生成される ``bundle.zip`` が壊れていないこと。"""
    import zipfile

    sample = _sample_dir(tmp_path)
    out = tmp_path / "out"
    code = main(
        [
            "solve",
            "--children",
            str(sample / "children.csv"),
            "--staff",
            str(sample / "staff.csv"),
            "--out",
            str(out),
            "--time-limit",
            "20",
            "--zip",
        ]
    )
    assert code in (EXIT_OK, EXIT_BLOCKER)
    bundle = out / "bundle.zip"
    assert bundle.exists()
    with zipfile.ZipFile(bundle) as zf:
        assert zf.testzip() is None
        assert "shift.csv" in zf.namelist()
        assert "shift.xlsx" in zf.namelist()


def test_seedで同じ入力なら同じ出力になる(tmp_path) -> None:
    """同じ入力なら、最適化が求まる範囲では出力が完全に一致すること。

    ``--seed`` はサンプルデータ生成の乱数シードであり、CBC 自体には影響しない。
    したがって **最適解が求まる規模の入力** なら、出力はビット単位で一致する。

    注意: 時間制限に間に合わず「実行可能解」で終わった場合は、CBC の探索が
    経過時間に左右されるため一致しない。溶けた後は Newport sulia 結果に依存するため、
    比較する問題は最適解が求まる規模に限っている。
    """
    day = _one_child_dir(tmp_path)

    def run(out_name: str, seed: str) -> str:
        out = tmp_path / out_name
        assert main(
            [
                "solve",
                "--children",
                str(day / "children.csv"),
                "--staff",
                str(day / "staff.csv"),
                "--start",
                "2026-09-28",
                "--end",
                "2026-09-28",
                "--out",
                str(out),
                "--time-limit",
                "60",
                "--seed",
                seed,
            ]
        ) in (EXIT_OK, EXIT_BLOCKER)
        return (out / "shift.csv").read_text(encoding="utf-8-sig")

    assert run("a", "7") == run("b", "7"), "同じ seed で再現性が保たれること"
