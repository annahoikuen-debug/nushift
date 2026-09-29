"""``shiftai`` コマンドラインインターフェース。

Streamlit アプリの起動に加え、ブラウザ無しで完結するヘッドレス処理を提供する。

サブコマンド:

* ``ui``      Streamlit アプリを起動する
* ``presets`` 自治体別の配置基準プリセットを一覧表示する
* ``sample``  サンプル CSV（園児・職員・希望休）を出力する
* ``template`` 空テンプレート CSV を出力する
* ``solve``   ヘッドレスで必要人員計算 → シフト最適化 → 過不足分析 → 出力を行う

終了コード:

===== ==========================================================
   0  正常終了（違反なし）
   1  入力・実行エラー
   2  法令違反（BLOCKER）が 1 件以上
   3  ``--strict`` 指定時かつ要調整（WARNING）が 1 件以上
       （INFO だけの違反は 0 件として扱い、終了コード 3 にはしない）
===== ==========================================================
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from shiftai import local_rules, sample_data
from shiftai.config import (
    APP_ICON,
    APP_TITLE,
    APP_VERSION,
    DEFAULT_DAY_CLOSE,
    DEFAULT_DAY_OPEN,
    DEFAULT_GRANULARITY_MIN,
)
from shiftai.data_loader import parse_time, read_bundle, write_template_csvs
from shiftai.domain import FacilitySettings, ViolationSeverity, daterange
from shiftai.exporter import (
    export_bundle_zip,
    payroll_dataframe,
    requirements_dataframe,
    shift_matrices,
    shift_to_dataframe,
    summary_markdown,
    to_ics,
)
from shiftai.gap_analysis import analyze_gap, check_violations
from shiftai.solver import solve_shift
from shiftai.standards import build_requirements, peak_requirement, total_required_hours

APP_FILE_NAME = "streamlit_app.py"
DEFAULT_STANDARD = "全国基準（厚労省）"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKER = 2
EXIT_WARNING = 3

_SAMPLE_NAMES = {
    "children": "children.csv",
    "staff": "staff.csv",
    "preferences": "preferences.csv",
}


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------


def _echo(message: str = "") -> None:
    print(message, flush=True)


def _error(message: str) -> None:
    print(f"エラー: {message}", file=sys.stderr, flush=True)


def _app_file() -> Path | None:
    """``streamlit_app.py`` の実体パスを探す。"""
    candidates = [
        Path.cwd() / APP_FILE_NAME,
        Path(__file__).resolve().parents[2] / APP_FILE_NAME,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _write_csv(frame: Any, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _parse_clock(value: str) -> Any:
    parsed = parse_time(value)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"時刻を解釈できません: {value!r}（例: 09:00）")
    return parsed


def _target_days(
    children_days: Sequence[date], start: str | None, end: str | None
) -> list[date]:
    """対象日を決める。``start``/``end`` が指定されていれば園児データと積む。"""
    days = sorted(set(children_days))
    if not days:
        return []
    first = _parse_day(start) if start else days[0]
    last = _parse_day(end) if end else days[-1]
    return [day for day in days if first <= day <= last]


# ---------------------------------------------------------------------------
# ui
# ---------------------------------------------------------------------------


def cmd_ui(args: argparse.Namespace) -> int:
    """Streamlit アプリを起動する。"""
    app = _app_file()
    if app is None:
        _error(
            f"{APP_FILE_NAME} が見つかりません。リポジトリのルートで実行するか、"
            "--app でファイルを指定してください"
        )
        return EXIT_ERROR
    target = Path(args.app) if args.app else app
    if not target.is_file():
        _error(f"アプリファイルがありません: {target}")
        return EXIT_ERROR

    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(target),
        "--server.port",
        str(args.port),
        "--server.address",
        args.address,
    ]
    if args.headless:
        command.append("--server.headless=true")
    _echo(f"{APP_ICON} {APP_TITLE} v{APP_VERSION} を起動します: {target}")
    try:
        return subprocess.run(command, check=False).returncode
    except OSError as exc:
        _error(f"streamlit を起動できませんでした: {exc}")
        return EXIT_ERROR


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------


def cmd_presets(args: argparse.Namespace) -> int:
    """プリセット一覧を表示する。"""
    presets = local_rules.list_presets()
    if args.json:
        _echo(json.dumps(presets, ensure_ascii=False, indent=2))
        return EXIT_OK
    for preset in presets:
        _echo(f"[{preset['key']}] {preset['name']}")
        _echo(f"    {preset['summary']}")
        _echo(f"    出典: {preset['source']}")
    _echo("")
    _echo(f"既定の基準は {DEFAULT_STANDARD} です（--standard で変更できます）。")
    return EXIT_OK


# ---------------------------------------------------------------------------
# sample / template
# ---------------------------------------------------------------------------


def cmd_sample(args: argparse.Namespace) -> int:
    """サンプル CSV を出力する。"""
    days: list[date] | None = None
    if args.start:
        first = _parse_day(args.start)
        days = [first + timedelta(days=i) for i in range(max(1, args.days))]
    target = Path(args.out)
    try:
        written = sample_data.write_sample_files(target, days=days, seed=args.seed)
    except OSError as exc:
        _error(f"サンプルを書き出せませんでした: {exc}")
        return EXIT_ERROR
    _echo(f"サンプルデータを出力しました（{len(days) if days else '既定'} 日分）:")
    for key, path in written.items():
        _echo(f"  {_SAMPLE_NAMES[key]:<18} {path}")
    return EXIT_OK


def cmd_template(args: argparse.Namespace) -> int:
    """空テンプレート CSV を出力する。"""
    try:
        written = write_template_csvs(Path(args.out))
    except OSError as exc:
        _error(f"テンプレートを書き出せませんでした: {exc}")
        return EXIT_ERROR
    _echo("テンプレートを出力しました:")
    for key, path in written.items():
        _echo(f"  {_SAMPLE_NAMES[key]:<18} {path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# solve
# ---------------------------------------------------------------------------


def _sample_days(args: argparse.Namespace) -> list[date] | None:
    """``--sample`` で生成する期間を決定する。

    以前は ``--start`` / ``--end`` を無視して既定の期間（今日から7日）を
    生成していたため、``solve --sample --start X --end X`` のように
    期間を絞ると「対象日が決まりませんでした」で何も出力されない。
    """
    if not args.start and not args.end:
        return None
    first = _parse_day(args.start) if args.start else date.today()
    last = _parse_day(args.end) if args.end else first
    if last < first:
        return None
    return list(daterange(first, last))


def cmd_solve(args: argparse.Namespace) -> int:
    """ヘッドレスでシフトを自動作成する。"""
    children_path = args.children
    staff_path = args.staff
    prefs_path = args.preferences

    if args.sample and (children_path or staff_path):
        _error("--sample は --children / --staff と併用できません")
        return EXIT_ERROR
    if args.sample or not (children_path and staff_path):
        if not args.sample:
            _error(
                "入力ファイルが足りません。--children と --staff を指定するか、"
                "動作確認の --sample を指定してください"
            )
            return EXIT_ERROR
        seed_dir = Path(args.out) / "sample"
        try:
            written = sample_data.write_sample_files(
                seed_dir, days=_sample_days(args), seed=args.seed
            )
        except OSError as exc:
            _error(f"サンプルデータを作れませんでした: {exc}")
            return EXIT_ERROR
        except ValueError as exc:
            _error(f"サンプルの期間を解釈できません: {exc}")
            return EXIT_ERROR
        children_path = written["children"]
        staff_path = written["staff"]
        prefs_path = prefs_path or written["preferences"]

    try:
        loaded = read_bundle(children_path, staff_path, prefs_path)
    except ValueError as exc:
        _error(str(exc))
        return EXIT_ERROR

    for issue in loaded.issues:
        prefix = "警告" if issue.level == "warning" else "エラー"
        _echo(f"[{prefix}] {issue}")

    if not loaded.staff:
        _error("職員が 1 人も読み込めませんでした")
        return EXIT_ERROR
    if not loaded.ok:
        # summary はメソッドなので呼ぶ（呼ばないと <bound method ...> の repr が出る）
        _error(loaded.summary())
        return EXIT_ERROR

    try:
        standard = local_rules.get_standard(args.standard)
    except KeyError as exc:
        _error(str(exc.args[0] if exc.args else exc))
        return EXIT_ERROR

    settings = FacilitySettings(
        facility_name=args.facility,
        day_open=args.open,
        day_close=args.close,
        granularity_min=args.granularity,
        closed_days=frozenset(_parse_day(d) for d in args.closed),
        holiday_dates=frozenset(_parse_day(d) for d in args.holiday),
    )

    days = _target_days([c.day for c in loaded.children], args.start, args.end)
    if not days:
        _error("対象日が決まりませんでした。--start / --end を確認してください")
        return EXIT_ERROR

    table = build_requirements(
        loaded.children,
        days,
        standard,
        day_open=settings.day_open,
        day_close=settings.day_close,
        granularity_min=settings.granularity_min,
        closed_days=settings.closed_days,
        holiday_dates=settings.holiday_dates,
        enforce_min_two=not args.no_min_two,
    )
    _echo(
        f"必要人員: {len(days)} 日 / {len(table.day_slots())} 時間帯 / "
        f"{total_required_hours(table):.1f} 人時（ピーク {peak_requirement(table)} 名）"
    )

    result = solve_shift(
        loaded.children,
        loaded.staff,
        table,
        preferences=loaded.preferences,
        settings=settings,
        time_limit_sec=args.time_limit,
        standard=standard,
    )
    _echo(f"最適化: {result.status.value}（{result.stats.get('elapsed_sec', 0.0)} 秒）")
    for message in result.messages:
        _echo(f"  {message}")

    slots = tuple(table.slots)
    report = analyze_gap(table, result, loaded.staff, standard=standard)
    violations = check_violations(
        table,
        result,
        loaded.staff,
        loaded.preferences,
        standard=standard,
        settings=settings,
        period_days=len(days),
    )

    out_dir = Path(args.out)
    written: list[Path] = [
        _write_csv(shift_to_dataframe(result, slots, loaded.staff), out_dir / "shift.csv"),
        _write_csv(payroll_dataframe(result, slots, loaded.staff, settings), out_dir / "payroll.csv"),
        _write_csv(shift_matrices(result, slots, loaded.staff), out_dir / "shift_matrix.csv"),
        _write_csv(requirements_dataframe(table), out_dir / "requirements.csv"),
        _write_csv(report.to_dataframe(), out_dir / "gap.csv"),
        _write_csv(report.daily_dataframe(), out_dir / "gap_daily.csv"),
    ]
    if violations:
        written.append(
            _write_csv(
                _violations_dataframe(violations),
                out_dir / "violations.csv",
            )
        )

    ics_path = out_dir / "shift.ics"
    ics_path.parent.mkdir(parents=True, exist_ok=True)
    ics_path.write_text(to_ics(result, slots, loaded.staff), encoding="utf-8")
    written.append(ics_path)

    summary_path = out_dir / "summary.md"
    summary_path.write_text(
        summary_markdown(
            result,
            slots,
            loaded.staff,
            facility_name=settings.facility_name,
            period=(days[0], days[-1]),
        ),
        encoding="utf-8",
    )
    written.append(summary_path)

    if args.zip:
        zip_path = out_dir / "bundle.zip"
        zip_path.write_bytes(
            export_bundle_zip(result, table, slots, loaded.staff, settings)
        )
        written.append(zip_path)

    _echo(f"配置カバー率: {report.coverage_ratio * 100:.1f}%（不足 {report.total_shortfall_slots} 時間帯）")
    for violation in violations:
        _echo(f"  [{violation.severity.value}] {violation.code}: {violation.message}")
    _echo(f"出力先: {out_dir}")
    for path in written:
        _echo(f"  {path.name}")

    if not result.ok:
        _error("シフトを作成できませんでした")
        return EXIT_ERROR
    blockers = [v for v in violations if v.severity is ViolationSeverity.BLOCKER]
    if blockers:
        return EXIT_BLOCKER
    # --strict は WARNING 以上（INFO は除く）で終了コード 3 を返す。
    # INFO は「参考」の扱いなので判定に含めない。
    actionable = [
        v for v in violations if v.severity in (ViolationSeverity.BLOCKER, ViolationSeverity.WARNING)
    ]
    if args.strict and actionable:
        return EXIT_WARNING
    return EXIT_OK


def _violations_dataframe(violations: Sequence[Any]) -> Any:
    return pd.DataFrame.from_records(
        [
            {
                "区分": v.severity.value,
                "コード": v.code,
                "日付": v.day.isoformat() if v.day else "",
                "職員ID": v.staff_id or "",
                "内容": v.message,
            }
            for v in violations
        ]
    )


# ---------------------------------------------------------------------------
# パーサ
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """引数パーサを作る。"""
    parser = argparse.ArgumentParser(
        prog="shiftai",
        description=f"{APP_ICON} {APP_TITLE} v{APP_VERSION}",
    )
    parser.add_argument("--version", action="version", version=f"shiftai {APP_VERSION}")
    sub = parser.add_subparsers(dest="command")

    ui = sub.add_parser("ui", help="Streamlit アプリを起動する")
    ui.add_argument("--port", type=int, default=8501, help="待ち受けポート（既定: 8501）")
    ui.add_argument("--address", default="localhost", help="待受アドレス（既定: localhost）")
    ui.add_argument("--headless", action="store_true", help="ブラウザを自動で開かない")
    ui.add_argument("--app", default=None, help="起動する Streamlit アプリのファイル")
    ui.set_defaults(func=cmd_ui)

    presets = sub.add_parser("presets", help="配置基準プリセットを一覧表示する")
    presets.add_argument("--json", action="store_true", help="JSON で出力する")
    presets.set_defaults(func=cmd_presets)

    sample = sub.add_parser("sample", help="サンプル CSV を出力する")
    sample.add_argument("--out", default="sample", help="出力先ディレクトリ（既定: sample）")
    sample.add_argument("--start", default=None, help="開始日 (YYYY-MM-DD)")
    sample.add_argument("--days", type=int, default=7, help="日数（既定: 7）")
    sample.add_argument("--seed", type=int, default=42, help="乱数シード（既定: 42）")
    sample.set_defaults(func=cmd_sample)

    template = sub.add_parser("template", help="空テンプレート CSV を出力する")
    template.add_argument("--out", default="template", help="出力先ディレクトリ（既定: template）")
    template.set_defaults(func=cmd_template)

    solve = sub.add_parser("solve", help="ヘッドレスでシフトを自動作成する")
    solve.add_argument("--children", default=None, help="園児 CSV / Excel / JSON")
    solve.add_argument("--staff", default=None, help="職員 CSV / Excel / JSON")
    solve.add_argument("--preferences", default=None, help="希望休 CSV / Excel / JSON")
    solve.add_argument("--standard", default=DEFAULT_STANDARD, help="配置基準プリセット名")
    solve.add_argument("--out", default="out", help="出力先ディレクトリ（既定: out）")
    solve.add_argument("--start", default=None, help="対象期間の開始日 (YYYY-MM-DD)")
    solve.add_argument("--end", default=None, help="対象期間の終了日 (YYYY-MM-DD)")
    solve.add_argument("--facility", default="あさひ保育園", help="園名")
    solve.add_argument(
        "--open", type=_parse_clock, default=DEFAULT_DAY_OPEN, help="開所時刻 (既定: 07:15)"
    )
    solve.add_argument(
        "--close", type=_parse_clock, default=DEFAULT_DAY_CLOSE, help="閉所時刻 (既定: 19:30)"
    )
    solve.add_argument(
        "--granularity", type=int, default=DEFAULT_GRANULARITY_MIN, help="時間帯の粒度（分）"
    )
    solve.add_argument("--closed", action="append", default=[], help="休園日 (YYYY-MM-DD)")
    solve.add_argument("--holiday", action="append", default=[], help="祝日・行事日 (YYYY-MM-DD)")
    solve.add_argument("--no-min-two", action="store_true", help="2名ルールを適用しない")
    solve.add_argument("--time-limit", type=int, default=60, help="ソルバの上限秒数（既定: 60）")
    solve.add_argument("--zip", action="store_true", help="成果物 ZIP も出力する")
    solve.add_argument("--strict", action="store_true", help="要調整があれば終了コード 3")
    solve.add_argument(
        "--sample", action="store_true", help="入力の代わりにサンプルデータを使う（動作確認用）"
    )
    solve.add_argument("--seed", type=int, default=42, help="サンプル生成の乱数シード")
    solve.set_defaults(func=cmd_solve)

    return parser


def _silence_broken_pipe() -> None:
    """パイプが閉じた後にブローアップ終了コード（120）にならないようにする。"""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except OSError:  # pragma: no cover - 環境依存
        pass


def main(argv: Sequence[str] | None = None) -> int:
    """エントリポイント。終了コードを返す。"""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    try:
        return int(args.func(args))
    except BrokenPipeError:
        _silence_broken_pipe()
        return EXIT_OK
    finally:
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            _silence_broken_pipe()


if __name__ == "__main__":
    raise SystemExit(main())
