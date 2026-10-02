"""エンドツーエンドの統合検証（UI スモーク + CLI ラウンドトリップ）。

このファイルは「**各モジュールは単体では動くが together に動かない**」problems を
的回帰として残すためのものです。既存の ``test_ui_tabs.py``（タブ描画の単体寄り）、
``test_e2e.py``（ライブラリ API 経由の業務フロー）、``test_cli.py``（サブコマンドの
終了コード）が担当していない層を補完し、**実際のアプリファイル ``streamlit_app.py`` を
AppTest で起動し、CLI を subprocess で最後まで通す**」层次を検証します。

2026-09-29 の E2E 検証で見つかった 2 件のバグは **修正済み**で、
下文のテストは通常の passing テストとして回帰の固定に徹している:

* ``shift.csv`` を ``--children`` に渡すと ``data_loader._iter_rows`` が
  ``KeyError: '職員ID'`` の**素の Traceback** で落ちていた。
  → 対応表に載らない列を無視する実装へ変更（``tests`` も更新）。
* 壊れた入力（登園日 ``25:00``、職員ID 空欄）で終了コード 1 のとき、
  エラーメッセージが ``<bound method LoadResult.summary of LoadResult(...)>``
  という**メソッドの repr** になっていた。
  → ``__main__.cmd_solve`` が ``loaded.summary()`` を呼ぶよう修正。
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from tests._utf8_subprocess import run_module_utf8

app_test = pytest.importorskip("streamlit.testing.v1", reason="AppTest が無い環境ではスキップ")

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "streamlit_app.py"

pytestmark = pytest.mark.timeout(900)


def _cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """``python -m shiftai`` を subprocess で実行する。

    出力は **UTF-8 固定** で読む。``text=True`` だけだとロケール
    （Windows では cp932 / cp1252）で日本語の出力をデコードすることになり、
    ``UnicodeDecodeError`` が内部リーダースレッドで起きて ``stdout`` が
    ``None`` になり、原因が隠れる。共通実装は ``tests/_utf8_subprocess.py``。
    """
    return run_module_utf8("shiftai", *args, cwd=cwd or ROOT, timeout=600)


def _exceptions(at) -> list[str]:
    return [str(e.value) for e in at.exception]


# ---------------------------------------------------------------------------
# 検証 1: アプリが実際に立ち上がり、5 タブすべてが描画される
# ---------------------------------------------------------------------------


def test_アプリが全タブを例外なく描画する():
    """素の状態（データ未投入）で ``streamlit_app.py`` 全体が 1 フレーム描画できる。

    既定は「シンプルモード」の 3 タブ（データ・シフト作成・出力）。
    タブ数をハードコードせず、**タブが 1 つ以上ありすべて例外が無い**ことで
    構成の妥当性を担保する。モードを切り替えた構成の確認は
    ``test_ui_tabs.py::test_上級者モードで5タブ構成に戻る`` が担う。
    """
    at = app_test.AppTest.from_file(str(APP), default_timeout=180).run()
    assert _exceptions(at) == []
    assert at.tabs, "タブが 1 つも描画されていない"
    assert at.title


def test_データ未投入では最適化ボタンを出さない():
    """職員 0 人の状態では「シフトを自動作成する」「必要人員を再計算」を出さない。

    案内が出るだけで、押せないため crash も起きないことを確認する。
    """
    at = app_test.AppTest.from_file(str(APP), default_timeout=180).run()
    labels = [b.label for b in at.button]
    assert not [lb for lb in labels if "自動作成する" in lb]
    assert not [lb for lb in labels if "再計算" in lb]


# ---------------------------------------------------------------------------
# 検証 2: 投入状態 → 必要人員 → シフト作成が UI 上で通る（重いので slow）
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_サンプル投入からシフト作成までUI上で通る():
    """3 表サンプル投入 → 読み込み → シフト作成を実操作で通す。

    ``AppTest`` のセッションは 1 プロセス内で維持されるため、同じ ``at`` に対して
    順にクリックする（``AppTest.from_file`` を作り直すと session_state が飛ぶ）。

    既定の「シンプルモード」は 3 タブ構成で、タブ 2「シフト作成」の中で
    必要人員を自動再計算するため、独立した「再計算」ボタンは出ない
    （不要に手動手順を増やさないため）。上級者モードで現れる「再計算」は
    あれば押すが、必須ではない。詳細は ``test_ui_tabs.py``。

    初期画面は入力方法を尋ねるため、先に「まとめて入力」を選んでから
    3 表の画面を操作する（ウィザード経由は ``test_ui_wizard`` が検証する）。
    """
    from shiftai.ui import wizard

    at = app_test.AppTest.from_file(str(APP), default_timeout=300).run()
    assert _exceptions(at) == []

    at.radio(key=wizard.KEY_MODE).set_value(wizard.MODE_BULK).run()
    at.button(key="wizard_start").click().run()
    assert _exceptions(at) == []

    for key in ("sample_children", "sample_staff", "sample_preferences"):
        at.button(key=key).click().run()
        assert _exceptions(at) == [], f"{key} の押下で例外"

    apply_btn = [b for b in at.button if "この内容で読み込む" in b.label]
    assert len(apply_btn) == 1
    apply_btn[0].click().run()
    assert _exceptions(at) == [], "読み込みで例外"

    # 上級者モードのときだけ出る「再計算」は、あれば押す。
    recompute = [b for b in at.button if "再計算" in b.label]
    if recompute:
        recompute[0].click().run()
        assert _exceptions(at) == [], "必要人員計算で例外"

    run = [b for b in at.button if "自動作成する" in b.label]
    assert len(run) == 1, "データ投入後にシフト作成ボタンが出ること"
    run[0].click().run()
    assert _exceptions(at) == [], "シフト作成で例外"

    # 結果が出た後の rerun と日付・プリセットの切り替えでも落ちない
    at.run()
    assert _exceptions(at) == []
    for select in at.selectbox:
        options = list(select.options)
        assert options
        select.set_value(options[-1]).run()
        assert _exceptions(at) == [], f"selectbox {select.label} の切替で例外"


# ---------------------------------------------------------------------------
# 検証 3: CLI の全サブコマンドと生成物の健全性（solve は重いので slow）
# ---------------------------------------------------------------------------


def test_presetsは一覧とJSONが一致する(tmp_path):
    text = _cli("presets")
    assert text.returncode == 0
    as_json = _cli("presets", "--json")
    assert as_json.returncode == 0

    import json

    presets = json.loads(as_json.stdout)
    assert len(presets) >= 1
    for preset in presets:
        assert preset["key"] in text.stdout
        assert preset["summary"] in text.stdout


def test_sampleとtemplateが3表を書き出す(tmp_path):
    for command, out in (("sample", tmp_path / "sample"), ("template", tmp_path / "tpl")):
        args = ["--out", str(out)]
        if command == "sample":
            args += ["--start", "2026-09-28", "--days", "3"]
        done = _cli(command, *args)
        assert done.returncode == 0, done.stderr
        for name in ("children.csv", "staff.csv", "preferences.csv"):
            path = out / name
            assert path.exists(), f"{command} が {name} を書き出さない"
            # BOM つき UTF-8 でも pandas が読めること
            assert not pd.read_csv(path).empty


@pytest.mark.slow
def test_solveの出力物が壊れていない(tmp_path: Path):
    """``solve --sample --zip`` が終了コード 0/2 のどちらかで終わり、成果物が読める。"""
    out = tmp_path / "out"
    done = _cli("solve", "--sample", "--out", str(out), "--time-limit", "30", "--zip")
    # BLOCKER があれば 2、法令違反がなければ 0。1（実行エラー）だけがNG。
    assert done.returncode in (0, 2), (done.returncode, done.stdout[-2000:], done.stderr[-2000:])

    for name in ("shift.csv", "payroll.csv", "requirements.csv", "gap.csv"):
        frame = pd.read_csv(out / name)
        assert not frame.empty, f"{name} が空"

    ics = (out / "shift.ics").read_text(encoding="utf-8")
    assert ics.startswith("BEGIN:VCALENDAR")
    assert ics.rstrip().endswith("END:VCALENDAR")

    # summary.md は UTF-8 で日本語が壊れていない
    summary = (out / "summary.md").read_text(encoding="utf-8")
    assert "対象期間" in summary
    assert "�" not in summary

    bundle = out / "bundle.zip"
    with zipfile.ZipFile(bundle) as zf:
        assert zf.testzip() is None, "bundle.zip が壊れている"
        assert "shift.xlsx" in zf.namelist()


@pytest.mark.slow
def test_shift_csvとpayrollの勤務時間が一致する(tmp_path: Path):
    """出力 CSV の内容が実際のシフトと整合しているか（サンプル 1 件）。"""
    out = tmp_path / "out_consistency"
    done = _cli("solve", "--sample", "--out", str(out), "--time-limit", "30")
    assert done.returncode in (0, 2)

    shift = pd.read_csv(out / "shift.csv")
    payroll = pd.read_csv(out / "payroll.csv")

    # 勤務・休憩以外は出ない
    assert set(shift["状態"].unique()) <= {"勤務", "休憩"}

    work = shift[shift["状態"] == "勤務"]
    # 時間帯は 30 分刻みなので「行数 × 0.5」が時間になる
    derived = work.groupby("職員ID").size() * 0.5
    merged = payroll[["職員ID", "総勤務時間"]].merge(
        derived.rename("派生"), on="職員ID", how="left"
    )
    # 勤務 0 日の職員は payroll 側にも 0 時間が入る（merge 後 NaN になる）
    merged["派生"] = merged["派生"].fillna(0.0)
    mismatched = merged[(merged["総勤務時間"] - merged["派生"]).abs() > 1e-6]
    assert mismatched.empty, f"勤務時間が一致しない職員: {mismatched.to_dict('records')}"


# ---------------------------------------------------------------------------
# 検証 4: 壊れた入力は日本語の理由つきエラーになる
# ---------------------------------------------------------------------------


def _write_sample(tmp_path: Path) -> Path:
    sample = tmp_path / "sample"
    done = _cli("sample", "--out", str(sample), "--start", "2026-09-28", "--days", "1")
    assert done.returncode == 0
    return sample


def test_壊れた入力は終了コード1でTracebackを出さない(tmp_path: Path):
    """``25:00`` の登園日・空の職員ID は終了コード 1 で日本語のエラーになる。

    以前は ``__main__.cmd_solve`` が ``_error(loaded.summary)`` と**メソッドを
    呼ばずに**渡しており、stdout に ``<bound method ...>`` 出ていた。
    ``loaded.summary()`` を呼ぶよう修正済み。
    """
    sample = _write_sample(tmp_path)

    broken = sample / "broken_children.csv"
    lines = (sample / "children.csv").read_text(encoding="utf-8-sig").splitlines()
    header = lines[0].split(",")
    assert "登園日" in header, f"想定外の列構成: {header}"
    idx = header.index("登園日")
    fields = lines[1].split(",")
    fields[idx] = "25:00"  # 存在しない日付
    lines[1] = ",".join(fields)
    broken.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    done = _cli(
        "solve",
        "--children",
        str(broken),
        "--staff",
        str(sample / "staff.csv"),
        "--out",
        str(tmp_path / "out"),
        "--time-limit",
        "10",
    )
    assert done.returncode == 1
    assert "Traceback" not in done.stderr, "内部 Traceback がそのまま出ている"
    assert "<bound method" not in done.stdout, "summary メソッドの repr が出ている"
    assert "読み込み" in done.stdout or "解釈" in done.stdout


def test_未知の列が混ざったCSVは読み込める():
    """期待しない列が 1 つ混ざっていても crashes せず警告として扱う。

    以前は ``data_loader._iter_rows`` が ``mapping[str(col)]`` を無条件に引いて
    マッピングされない列があると ``KeyError`` で落ちていた。
    未知の列を無視し警告を 1 件出す実装へ修正済み。
    """
    from shiftai import data_loader

    frame = pd.DataFrame(
        [
            {
                "園児ID": "C1",
                "氏名": "テスト",
                "年齢": 3,
                "登園日": "2026-09-28",
                "登園時刻": "09:00",
                "降園時刻": "15:00",
                "短時間保育": "false",
                "欠席": "false",
                "欠席理由": "",
                "早朝保育": "false",
                "延長保育": "false",
                "備考": "",
                "renov": "unknown-column",
            }
        ]
    )
    plans, issues = data_loader.load_children(frame)
    assert plans, "無関係な列があっても読み込みは通ること"
    warnings = [i for i in issues if i.level == "warning"]
    assert warnings
    assert any("renov" in i.message for i in warnings), [i.message for i in warnings]


def test_shift_csvは入力として再利用できない(tmp_path: Path):
    """``shift.csv`` は出力専用なので入力にはできない。礼貌エラーになる。

    以前は ``KeyError: '職員ID'`` の Traceback で落ちていた。
    未知列を無視する実装へ修正済みで、行毎のエラーとして日本語で報告される。

    以前は別のテストの出力（``/tmp/e2e/test_out``）に依存しており、
    ``-m "not slow"`` では常にスキップされていた。ここでは自前で
    ``shift.csv`` 相当の CSV を作って渡すため、単体で実行できる。
    """
    out = tmp_path / "out"
    done = _cli("solve", "--sample", "--out", str(out), "--time-limit", "30")
    assert done.returncode in (0, 2)
    shift_csv = out / "shift.csv"
    assert shift_csv.exists(), "shift.csv が生成されていない"

    done = _cli(
        "solve",
        "--children",
        str(shift_csv),
        "--staff",
        str(out / "sample" / "staff.csv"),
        "--out",
        str(tmp_path / "reuse_out"),
        "--time-limit",
        "10",
    )
    assert done.returncode == 1
    assert "Traceback" not in done.stderr, "内部 Traceback がそのまま出ている"
    assert "Traceback" not in done.stdout, "内部 Traceback がそのまま出ている"
