# 回帰防止テスト計画書 — NuShift (Project B)

バージョン: NuShift v0.4.1  
作成日: 2026-10-31  
対象リポジトリ: `E:\nushift\nushift\`

---

## 1. テスト戦略

### 1.1 単体テスト（関数・クラス単位）
ドメインモデルの値取得・不正値拒否、純粋関数を検証する。
対象: `shiftai.domain`（AgeClass, ChildPlan, StaffMember, Contract, Slot, など）、`shiftai.exporter`（CSV/Excel/iCal 出力）、`shiftai.gantt_pdf`（PDF 出力）、`shiftai.compliance`（コンプライアンス監査）。

### 1.2 結合テスト（ソルバー、配置基準、ギャップ分析）
ソルバー（HiGHS/CBC）、配置基準エンジン、ギャップ分析との連携を検証する。
対象: `shiftai.solver.solve_shift`, `shiftai.standards.build_requirements`, `shiftai.gap_analysis.analyze_gap`.

### 1.3 E2E テスト（主要ユーザーフロー）
データ読み込み → 基準適用 → シフト最適化 → 違反検出 → エクスポート（Excel/CSV/PDF/iCal）の一連フローを検証する。
対象: `tests/test_e2e.py`, `tests/test_ui_steps.py`, `tests/test_ui_wizard.py`.

### 1.4 セキュリティテスト（認証、認可、レート制限、ヘッダー）
Streamlit アプリの認証状態遷移、アクセス制御、入力サニタイズを検証する。
対象: `tests/test_security_hardening.py`, `tests/test_ui_state.py`.

### 1.5 パフォーマンス・回帰テスト（シフト求解時間、メモリリーク）
- HiGHS/CBC 求解時間の上限遵守（time_limit_sec）
- 大規模データ（28名職員・1週間）での求解時間とメモリ使用量
- ウォームスタートによる求解時間短縮効果

---

## 2. テストカテゴリと対象（Project B）

| カテゴリ | 対象モジュール・機能 | 検証項目 |
|---|---|---|
| ソルバー | HiGHS / CBC 求解、制約充足、ウォームスタート | 求解成功、実行可能解、制約充足、ウォームスタート、インフェジブル検出 |
| ドメインモデル | AgeClass, ChildPlan, StaffMember, Contract | 値取得、不正値拒否（ValueError）、等価比較、シリアライズ |
| 配置基準 | 厚労省基準、東京都基準、福岡市基準、カスタム基準 | 必要人員計算、年齢クラス別配置、基準切り替え |
| ギャップ分析 | 配置割れ検知、違反検出 | 過不足検出、違反コード一致、重要度分類 |
| UI | Streamlit コンポーネント、タブ遷移、ウィザードフロー | AppTest によるレンダリング、タブ切り替え、ウィザードステップ遷移 |
| エクスポート | Excel, CSV, PDF, iCal | 出力形式の妥当性、BOM 付き CSV、openpyxl 読み込み、iCal 形式、PDF バイト列 |
| コンプライアンス | 労基法第32条、36協定、変形労働制、割増賃金 | 労働時間上限、時間外労働制限、変形労働時間制、割増賃金計算 |

---

## 3. テストファイル構成

### 3.1 既存テストファイル（主要）
- `tests/test_solver.py` — ソルバー（小規模・ハード制約検証）
- `tests/test_domain.py` — ドメインモデル
- `tests/test_standards.py` — 配置基準
- `tests/test_gap_analysis.py` — ギャップ分析
- `tests/test_exporter.py` — CSV/Excel/iCal エクスポート
- `tests/test_compliance.py` — コンプライアンス監査
- `tests/test_ui_components.py` — UI コンポーネント
- `tests/test_ui_tabs.py` — UI タブ統合テスト
- `tests/test_e2e.py` — E2E テスト
- `tests/test_solver_perf.py` — ソルバー性能（既知のインポートエラーあり）

### 3.2 新規回帰テストファイル（本計画で追加）

| ファイル | カテゴリ | 主要検証項目 |
|---|---|---|
| `tests/test_regression_solver.py` | ソルバー | HiGHS/CBC 求解、制約充足、実行可能解、ウォームスタート |
| `tests/test_regression_compliance.py` | コンプライアンス | 労基法第32条、36協定、変形労働制、割増賃金 |
| `tests/test_regression_export.py` | エクスポート | Excel, CSV, PDF, iCal 出力 |
| `tests/test_regression_ui.py` | UI | Streamlit コンポーネント、タブ遷移 |

---

## 4. CI/CD 統合

### 4.1 プリコミットフック
- `ruff check .` — コミット前に実行。E, F, I, UP, B ルールを適用（E501, B008, UP042 は除外）。

### 4.2 プルリクエスト時のテスト実行
- プルリクエスト作成・更新時に `make test-fast`（`-m "not slow"`）を実行。
- 新規テストファイル（`test_regression_*.py`）も含めて全テストがパスすることをマージ条件とする。
- slow マーカー付きテストは `make test-slow` で別途実行。

### 4.3 カバーレーショ閾値（80%以上）
- `pytest-cov` を用いて `shiftai` パッケージのカバレッジを測定。
- 目標: カバレッジ 80% 以上。
- `make cov` で `term-missing` 形式でレポート出力。

### 4.4 回帰テストの自動実行スケジュール
- 毎日定時に `make test`（フルテスト）を自動実行。
- 週次で `make test-slow`（slow マーカー付き統合テスト）を実行。
- 失敗検出時に通知を送信。

---

## 5. カバレッジ目標

| カテゴリ | 目標カバレッジ |
|---|---|
| ソルバー | 85% 以上 |
| コンプライアンス | 90% 以上 |
| エクスポート | 90% 以上 |
| UI | 80% 以上 |
| 全体 | 80% 以上 |

---

## 6. テスト実行コマンド

```bash
# 高速テスト（slow を除外）
make test-fast

# 全テスト（slow を含む）
make test

# slow テストのみ
make test-slow

# 並列実行
make test-parallel

# カバレッジ付き全テスト
make cov

# リンタチェック
make lint

# 個別ファイル実行
python -m pytest tests/test_regression_solver.py -q
python -m pytest tests/test_regression_compliance.py -q
python -m pytest tests/test_regression_export.py -q
python -m pytest tests/test_regression_ui.py -q
```

---

## 7. 受け入れ基準

- [ ] 新規回帰テストファイル 4 つすべてがパスする
- [ ] 既存テストと整合し、回帰がないこと
- [ ] `ruff check .` がエラーゼロ
- [ ] カバレッジ 80% 以上（新規テスト追加後）
