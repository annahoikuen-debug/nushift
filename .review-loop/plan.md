# レビュー指摘の修正計画（review-loop）

作成日: 2026-11-16
出所: 2026-11-16 のコードレビュー（4領域並列レビュー + 実行検証）

## 実行時のベースライン

- `ruff check .` → 37 errors
- `pytest -m "not slow"` → 4 failed / 1378 passed
- `shiftai solve --sample` → PARTIAL（配置不足あり）

ラウンドごとの完了条件は「対象テストが緑」+「既存テストを壊さない」。

---

## Round 1 — CI を緑に戻す（Blocker）

完了条件: `ruff check .` が 0 件、かつ以下の 4 テストが緑。

1. lint 37 件（`ruff check .` が通るまで）
   - `src/shiftai/ui/components.py:47,58` — `FALLBACK_VIOLATION_LABELS` の
     `"DAILY_HOURS_EXCEEDED"` 重複キー。後勝ちで上書きされているため、
     より具体的な文言（契約上限を超えています）を残し片方を削除。
   - 未使用 import / import 順 / B007 / F811 / F541 / F841 / E402 は
     `ruff check --fix` + 手修正。**`ruff format` は実行しない**（70ファイルが
     再整形され差分が爆発する。CI も format を非ブロッキングにしている）。
2. `docs/15_実装計画_第2_3段階_求解性能.md` — 存在しない `bench7.py` への言及を修正
   （実ファイルは `scripts/bench_kpi.py`。無いものは文面から削る）。
3. `docs/16_実装計画_レビュー指摘修正.md` / `docs/17_実装計画_K1_Blocker解消.md` —
   `bench_kpi.py` の記載を `scripts/bench_kpi.py` に修正。
4. `docs/05_開発ガイド.md` — CLI オプション表に `--engine` / `--mode` /
   `--monthly-holiday-target` / `--workers` を実装（`build_parser`）どおり追記。
5. `docs/15,16,17` — テスト件数のハードコードを除去
   （`test_static_guards::test_docsにテスト件数がハードコードされていない` が
   検出する行のみ。数字+件/passed/failed を含む文を「全件 pass」等の表現へ）。
6. `tests/test_practical_solve_performance.py:60` — 15 秒しきい値を 60 秒へ。
   根拠: docs/17 の K1 目標（wall 60 秒以内）。計測は環境差（CPU/HiGHS 版）で
   変動し、15 秒はこの環境で 20 秒かかって失敗する。
   回帰検知の本体は `fallback is None` と違反 0 件のアサーションなので、
   時間しきい値は「180 秒タイムアウト転落」を検知できる幅でよい。
   コメントに環境差の旨を記す。**その他のアサーションは緩めない**。

検証: `ruff check .`、`pytest tests/test_docs_claims.py tests/test_static_guards.py
tests/test_practical_solve_performance.py -q`

---

## Round 2 — 正しさの修正（コア）

1. `src/shiftai/score.py`
   - `SHORTFALL_QUALIFIED` の再計算が `standard` を無視して保育士のみを数える
     （`nurse_as_qualified_cap` / `qualified_extra_roles` 未対応）。
     `domain.qualified_ids` / `domain.qualified_count` に委譲する。
     `compute_shift_score` に `standard: StaffingStandard | None = None` を追加し、
     呼び出し元（`monthly.py` など）から渡す。`standard=None` のときは現行挙動。
   - `OVERSTAFF` の二重計上: violations 由来で `matches` に既にある場合は
     再集計セクションをスキップ。
2. `src/shiftai/live_validation.py:389-415` — `is_placeable` でない職員を
   配置人数・保育士数に数えない（`gap_analysis` と一致させる）。
3. `src/shiftai/monthly.py`
   - `pat_keys` の重複排除（同一 key が複数パターンで使われたら明示エラー）。
   - `week_fixed[k] = CellState.WORK` がユーザーの手動 OFF 固定を上書きしない。
   - 並列求解でチャンクが例外の場合、その日の `ShiftDay` が無言で欠落する。
     全 OFF の ShiftDay を補い、メッセージに原因を残す（status は既に PARTIAL）。
4. `src/shiftai/solver.py`
   - `_staff_signature` に `has_36_agreement` / `variable_working_hours` /
     `break_minutes` / `min_daily_hours` / `max_daily_hours` /
     `max_work_blocks_per_day` を追加（対称性破りの前提を正しくする）。
   - `_greedy` で `solve_shift_greedy` を try し、例外時は ERROR の SolveResult
     を返す（最後の砦が例外で落ちないように）。
5. `src/shiftai/validation.py:353-355` — 「同じか後になっています」→「同じか前」。

検証: `pytest tests/test_score*.py tests/test_monthly*.py tests/test_solver*.py
tests/test_validation*.py tests/test_live_validation.py tests/test_fairness*.py -q`
（存在するファイルのみ）+ 最後に fast 全套。

---

## Round 3 — UI の状態管理

1. `src/shiftai/ui/state.py` — `WIDGET_KEYS` にサイドバーの実ウィジェットキーを
   追加（`closed_days` / `holiday_dates` / `break_minutes` /
   `max_work_blocks_per_day` / `sei_min_daily_hours` / `sei_max_daily_hours` /
   `ui_time_limit_sec` ほか、`sidebar.py` を grep して漏れをすべて埋める）。
   「すべて初期化」後に旧設定が復活しないこと。
2. `src/shiftai/ui/tab_solve.py` + `state.py` — `shortfall_ack` を
   `DEFAULT_KEYS`（既定 False）に入れ、入力変更の検知
   （`mark_inputs_changed` / `invalidate_pipeline`）で解除する。
   一度同意したら以後永久にゲートが出ない状態をなくす。
3. `src/shiftai/ui/tab_shift.py:398` — `date.fromisoformat` を try で守る
   （不正な `KEY_FIX_TARGET` で描画が落ちない）。

検証: `pytest tests/test_ui*.py tests/test_e2e_ui_smoke.py -q` + fast 全套。

---

## Round 4 — Medium/Low のうち安全なもの

1. `src/shiftai/importers.py` — `_value`/`_text` が `pd.NA`/`pd.NaT` を
   文字列化しない（`pd.isna`）。`short_text == ""` → `not short_text`。
2. `src/shiftai/local_rules.py:421,459` — 分「15」のハードコードをやめ、
   `_FACILITY_OPEN` から分を引き継いで 11 時間加算する。
3. `src/shiftai/exporter.py:609` — `FMTTYPE=text` → `text/plain`（テストの
   文字列アサーションがあれば同時に更新）。
4. 文言・デッドコード整理
   - `src/shiftai/ui/tab_export.py:470`「戻すadion_operation」
   - `src/shiftai/config.py:77`「ántoん」
   - `src/shiftai/fairness.py:214` 中国語混入
   - `src/shiftai/data_loader.py:145` `" choosable"`
   - `src/shiftai/importers.py:45`「R6 = 2026」→ R6 = 2024
   - `src/shiftai/gas_client.py:310` mode のメッセージ
   - `tests/test_ui.py` の「UI 実装待ち」skip ガード撤去
   - `README.md`（`(70ファイル)` の件数削除、`interval_candidate_limit=400` の
     記載を実態どおりに、`完全抑止` の表現を緩和）、`Q_AND_A.md` 同項
5. `landing_page/app.js` — ダミー送信の撤去。送信していないのに
   「受け付けました／メールをお送りします」と表示しない。
   実送信先が無い旨と問い合わせ手段を明示する誠実な文言へ。

検証: `pytest -m "not slow" -q`（fast 全套）+ `ruff check .`

---

## ループ外（要人間判断 — コードは触らない）

- **High-2**: `local_rules.py` の「全国基準」3歳 8:1 / 4・5歳 20:1 が
  厚生省告示第49号（3歳 20:1 / 4・5歳 30:1 と一般に解される）と食い違い、
  README（15:1/20:1、25:1/30:1）とも不一致。法源の一次資料で確認するまで
  数値を自動修正しない。`tests/test_standards.py:91` が現状の誤値（要確認）を固定。
- **High-3**: 認可外の保育士比率が `local_rules`（1/3）と `compliance`（1/2）で
  二重定義。どちらが正かは法解釈の確認が必要（単一ソース化は値確定後）。
- **M1**: `daily_cap_minutes` の 1.25 倍（割増賃金率の時間への転用）。
  テストが現行式を固定しているため設計判断が必要。
- **M2**: `payroll` の「推定人件費 / 割増人件費」の列定義（基底が不一致）。
  出力スキーマの変更を伴うため利用者影響を確認してから。
- **M12**: `filterwarnings = ignore::DeprecationWarning` の全抑制解除。
  警告が大量に出る可能性があり別作業とする。
- **M6 の警告追加**（区間列挙上限超過時のサイレント制約解除）:
  stats への記録は Round 2 で対応可能だが、表示設計を要するため見送り。
