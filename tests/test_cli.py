"""``shiftai.__main__``（コマンドライン）のテスト。

``main(argv)`` は終了コードを返す設計なので、サーバーを起動せず引数だけ差し替えられる。
``ui`` サブコマンドは実プロセスを起動するため、ここでは実行しない。
"""

from __future__ import annotations

import json

import pytest

from shiftai.__main__ import EXIT_ERROR, EXIT_OK, main

SLOW = pytest.mark.slow


def test_引数無しでヘルプ表示(capsys):
    """サブコマンド無しではヘルプを出して正常終了する。"""
    assert main([]) == EXIT_OK
    assert "usage: shiftai" in capsys.readouterr().out


def test_presets_jsonは解釈できる(capsys):
    """プリセット一覧が JSON として取り出せること。"""
    assert main(["presets", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    keys = {item["key"] for item in payload}
    assert "全国基準（厚労省）" in keys
    assert all(item["summary"] and item["source"] for item in payload)


def test_presetsは表形式で表示される(capsys):
    """表形式でもプリセット名と出典が出ること。"""
    assert main(["presets"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "[全国基準（厚労省）]" in out
    assert "出典:" in out


def test_sampleはCSV3枚を書き出す(tmp_path, capsys):
    """``--days`` を指定するとその日数だけ生成されること。"""
    out = tmp_path / "sample"
    assert main(["sample", "--out", str(out), "--start", "2026-09-28", "--days", "2"]) == EXIT_OK
    assert (out / "children.csv").is_file()
    assert (out / "staff.csv").is_file()
    assert (out / "preferences.csv").is_file()
    days = {
        line.split(",")[3]
        for line in (out / "children.csv").read_text(encoding="utf-8-sig").splitlines()[1:]
    }
    assert days == {"2026-09-28", "2026-09-29"}
    assert "サンプルデータを出力しました" in capsys.readouterr().out


def test_sampleはシードで決定的(tmp_path):
    """同じシードなら同じ CSV になること。"""
    first, second = tmp_path / "a", tmp_path / "b"
    for target in (first, second):
        assert (
            main(["sample", "--out", str(target), "--start", "2026-09-28", "--days", "1"])
            == EXIT_OK
        )
    assert (first / "children.csv").read_bytes() == (second / "children.csv").read_bytes()


def test_templateは1行ずつのCSVを書き出す(tmp_path):
    """テンプレートはヘッダ + 1 行だけであること。"""
    out = tmp_path / "tpl"
    assert main(["template", "--out", str(out)]) == EXIT_OK
    for name in ("children.csv", "staff.csv", "preferences.csv"):
        assert (out / name).is_file()
    assert len((out / "children.csv").read_text(encoding="utf-8-sig").splitlines()) == 2


def test_solveは入力不足でエラー(tmp_path, capsys):
    """``--children`` / ``--staff`` が無く ``--sample`` も無ければ終了コード 1。"""
    assert main(["solve", "--out", str(tmp_path / "out")]) == EXIT_ERROR
    assert "入力ファイルが足りません" in capsys.readouterr().err


def test_solveはsampleと実入力を併用できない(tmp_path, capsys):
    """``--sample`` と ``--children`` の併用は弾く。"""
    assert main(["solve", "--sample", "--children", "a.csv", "--out", str(tmp_path)]) == EXIT_ERROR
    assert "併用できません" in capsys.readouterr().err


def test_solveは不明な基準を弾く(tmp_path, capsys):
    """存在しないプリセット名は終了コード 1 で弾かれる。"""
    tpl = tmp_path / "tpl"
    assert main(["template", "--out", str(tpl)]) == EXIT_OK
    code = main(
        [
            "solve",
            "--children",
            str(tpl / "children.csv"),
            "--staff",
            str(tpl / "staff.csv"),
            "--standard",
            "存在しない基準",
            "--out",
            str(tmp_path / "out"),
        ]
    )
    assert code == EXIT_ERROR
    assert "不明なプリセットキー" in capsys.readouterr().err


@SLOW
def test_solve_サンプルで成果物が揃う(tmp_path, capsys):
    """ヘッドレス実行で成果物一式が書き出されること。"""
    out = tmp_path / "out"
    code = main(
        [
            "solve",
            "--sample",
            "--out",
            str(out),
            "--start",
            "2026-09-28",
            "--end",
            "2026-10-02",
            "--open",
            "09:00",
            "--close",
            "14:00",
            "--granularity",
            "60",
            "--time-limit",
            "30",
            "--zip",
        ]
    )
    assert code in (EXIT_OK, 2, 3)
    text = capsys.readouterr().out
    assert "必要人員:" in text
    assert "最適化:" in text
    assert "配置カバー率:" in text
    for name in (
        "shift.csv",
        "payroll.csv",
        "shift_matrix.csv",
        "requirements.csv",
        "gap.csv",
        "gap_daily.csv",
        "shift.ics",
        "summary.md",
        "bundle.zip",
    ):
        assert (out / name).is_file(), name
    assert (out / "shift.ics").read_text(encoding="utf-8").startswith("BEGIN:VCALENDAR")


def test_勤務パターンの定義文字列は解釈できる(capsys, tmp_path):
    """``--patterns`` が不正なら終了コード 1 で弾かれること。

    修正前: ``--out "out"``（相対パス）だったため、
    ``cmd_solve`` が ``--patterns`` を検証する **前に**
    サンプルファイルを ``./out/sample/`` に書き込んでいた。
    テスト実行ディレクトリ（リポジトリ直下）が汚染され、
    ``make test-parallel`` と同時実行の ``make solve`` が衝突する。
    """
    code = main(["solve", "--sample", "--patterns", "壊れた定義", "--out", str(tmp_path / "out")])
    assert code == EXIT_ERROR
    assert "パターン" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--closed", "2026-13-45"),
        ("--closed", "2026-02-30"),
        ("--holiday", "abc"),
        ("--holiday", "2026-99-01"),
        ("--start", "not-a-date"),
        ("--end", "2026-01-01"),
    ],
)
def test_不正な日付はTracebackではなく終了コード1になる(flag, value, capsys, tmp_path):
    """``--closed`` / ``--holiday`` / ``--start`` / ``--end`` の不正値は
    traceback を出さず、終了コード 1 と日本語のエラーメッセージにすること。

    修正前: ``_parse_day`` の ``ValueError`` が ``try`` の外で伝播し、
    利用者に Python の traceback (``month must be in 1..12``) が出ていた。
    """
    code = main(["solve", "--sample", flag, value, "--out", str(tmp_path / "out")])
    err = capsys.readouterr().err
    assert code == EXIT_ERROR, f"{flag}={value} で終了コードが {code}"
    assert "Traceback" not in err, f"traceback が出ている:\n{err}"
    assert "エラー" in err, f"日本語のエラーメッセージが無い:\n{err}"


@pytest.mark.parametrize(
    "spec",
    ["早番=09:00-14:00", "auto", "default"],
)
def test_勤務パターンの指定は通る(spec: str) -> None:
    """代表的な定義がパースできること（求解は行わない）。"""
    from shiftai.__main__ import _parse_patterns
    from shiftai.domain import FacilitySettings

    patterns = _parse_patterns(spec, FacilitySettings())
    assert patterns
    assert all(p.end_minutes > p.start_minutes for p in patterns)


@SLOW
def test_solve_勤務パターンを渡すと整列結果が出る(tmp_path, capsys):
    """``--patterns`` ありなら整列の内訳が出力されること。"""
    out = tmp_path / "out"
    code = main(
        [
            "solve",
            "--sample",
            "--out",
            str(out),
            "--start",
            "2026-09-28",
            "--end",
            "2026-09-29",
            "--open",
            "09:00",
            "--close",
            "14:00",
            "--granularity",
            "60",
            "--time-limit",
            "20",
            "--patterns",
            "早番=09:00-14:00,日勤=10:00-13:00",
            "--diagnose",
        ]
    )
    assert code in (EXIT_OK, 2, 3)
    text = capsys.readouterr().out
    assert "勤務パターン:" in text
    assert "勤務パターンの整列:" in text
    assert "なぜこの結果になったのか" in text


@SLOW
def test_solve_緩和モードは結果を返す(tmp_path, capsys):
    """``--relax`` を指定しても成果物が揃い、緩和の内容が示されること。"""
    out = tmp_path / "out"
    code = main(
        [
            "solve",
            "--sample",
            "--out",
            str(out),
            "--start",
            "2026-09-28",
            "--end",
            "2026-09-29",
            "--open",
            "09:00",
            "--close",
            "14:00",
            "--granularity",
            "60",
            "--time-limit",
            "20",
            "--relax",
            "1",
        ]
    )
    assert code in (EXIT_OK, 2, 3)
    text = capsys.readouterr().out
    assert "緩和モード: L1" in text
    assert "緩めた制約:" in text
    assert (out / "shift.csv").is_file()
