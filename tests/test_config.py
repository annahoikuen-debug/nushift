"""``shiftai.config`` の回帰テスト（T-10-R4）。

法定要件に基づく定数が変わると、適合判定そのものが静かに変わってしまう。
数値をここで固定し、意図的に変更する場合はこのテストで気付けるようにする。
"""

from __future__ import annotations

from datetime import date, time

from shiftai import config


def test_休憩の法定閾値が変わらない() -> None:
    """6時間超が45分、8時間超が60分であること（労働基準法第9条）。"""
    assert config.STATUTORY_BREAK_THRESHOLDS == ((8 * 60, 60), (6 * 60, 45))
    assert config.STATUTORY_BREAK_MINUTES == 45


def test_休息時間の法定下限が変わらない() -> None:
    """勤務間の最低休息時間は11時間であること（労働基準法第9条）。"""
    assert config.STATUTORY_MIN_REST_HOURS == 11


def test_日勤の上限が変わらない() -> None:
    assert config.STATUTORY_DAILY_WORK_HOURS == 8


def test_週労働時間の上限は厳しい側にある() -> None:
    """週44時間は2018年改正で失効した古い方（厳しい方）を使うこと。

    労働基準法第32条の4 は2019-04-01 の改正で「月45時間・年360時間」に
    変更され、旧来の「週44時間」は法定の上限ではなくなった。制約としては
    厳しい方を使うため法令違反は生じないが、**緩める方向的**な変更は
    禁じる。緩めた時点でこのテストが落ちる。
    """
    assert config.STATUTORY_WEEKLY_WORK_HOURS == 44
    assert config.STATUTORY_WEEKLY_WORK_HOURS < 45, (
        "45 に緩めると旧基準になり得るため、44 未満で一定にする"
    )


def test_法定上限の順序が変わらない() -> None:
    assert (
        config.STATUTORY_DAILY_WORK_HOURS
        <= config.STATUTORY_WEEKLY_WORK_HOURS
        <= config.STATUTORY_OVERTIME_LIMIT_HOURS
    )


def test_休憩の既定値が変わらない() -> None:
    assert config.DEFAULT_BREAK_MINUTES == 60
    assert config.DEFAULT_BREAK_STAGGER_MINUTES == 30
    assert config.DEFAULT_GRANULARITY_MIN == 30


def test_園の開設時間の既定が変わらない() -> None:
    assert config.DEFAULT_DAY_OPEN == time(7, 15)
    assert config.DEFAULT_DAY_CLOSE == time(19, 30)
    assert config.DEFAULT_SATURDAY_OPEN == time(7, 30)
    assert config.DEFAULT_SATURDAY_CLOSE == time(18, 30)


def test_表示用配色が全て指定通りである() -> None:
    assert config.COLOR_WORK.startswith("#")
    assert config.COLOR_BREAK.startswith("#")
    assert config.COLOR_OFF.startswith("#")
    assert config.COLOR_SHORTFALL.startswith("#")
    assert config.COLOR_OVER.startswith("#")
    assert config.COLOR_WORK != config.COLOR_BREAK, "勤務と休憩の色が区別できること"


def test_既定表示期間の日数が変わらない() -> None:
    assert config.DEFAULT_RANGE_DAYS == 7
    assert isinstance(config.DEFAULT_RANGE_START, date)


def test_アプリの識別情報が埋まっている() -> None:
    assert config.APP_TITLE
    assert config.APP_VERSION.count(".") >= 1


# --- T-13: 表記の統一 -------------------------------------------------------


def test_週上限定数に法的経緯がコメントにある() -> None:
    """定数のコメントに失効の経緯が書かれていないと UI の文言が断定してしまう。"""
    import inspect

    from shiftai import config as config_module

    source = inspect.getsource(config_module)
    assert "2019" in source, "2019年の改正について触れていること"
    assert "失効" in source or "法定の上限ではなく" in source, "旧基準の失効に触れていること"
    assert "月45時間" in source or "月 45 時間" in source, "現行の枠組みを述べていること"


def test_UIが旧基準を法定と断定しない() -> None:
    """UI の表示文言に「労働基準法の週法定労働時間は 44 時間」が残っていないこと。"""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "shiftai"
    offenders = [
        f"{path.relative_to(src)}:{i}"
        for path in sorted(src.rglob("*.py"))
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "週法定労働時間は" in line and "44" in line
    ]
    assert not offenders, f"旧基準を法定と断定している: {offenders}"


def test_UIに内部目安である旨が表示される() -> None:
    """週44時間を内部の目安として示す文言が UI にあること。"""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "shiftai" / "ui"
    body = "\n".join(path.read_text(encoding="utf-8") for path in sorted(src.rglob("*.py")))
    assert "内部目安" in body or "内部の目安" in body, (
        "「内部の目安」であることが UI に示されていること"
    )
