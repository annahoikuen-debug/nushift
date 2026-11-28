# ⚡ NuShift (shiftai v0.9.0)

> **保育のすべてをつなぎ、先生の時間をこどもたちへ**  
> 職員配置基準連動型 数理最適化シフト自動作成エンジン（NuShift）と、直感的な Streamlit UI / React 月間シフトエディタによる保育園・こども園向け勤務シフト自動生成システム。

[![Version](https://img.shields.io/badge/version-v0.9.0-blue.svg)](pyproject.toml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.13-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](pyproject.toml)
[![Solver: HiGHS 推奨](https://img.shields.io/badge/Solver-HiGHS%20推奨-blue.svg)](src/shiftai/solver.py)
[![CI](https://img.shields.io/badge/CI-Tests%20%7C%20Lint-blueviolet.svg)](.github/workflows/tests.yml)
[![Community](https://img.shields.io/badge/Community-Discussions-9cf.svg)](https://github.com/hoikuict/nushift/discussions)

**[操作マニュアル](MANUAL.md) | [Q&A (よくある質問)](Q_AND_A.md) | [ランディングページ](landing_page/index.html) | [開発ガイド](docs/05_開発ガイド.md) | [データ仕様](docs/04_データ仕様.md)**

---

## 1. NuShift (shiftai) とは

**NuShift** は、厚生労働省の保育所等職員配置基準および各自治体（東京都・福岡市等）の独自基準・加算基準に完全対応した、**職員配置基準連動型の数理最適化シフト自動作成エンジン**です。

現場の主任保育士が毎月数日〜1週間かけて作成・微調整していた複雑な勤務シフト表を、混合整数線形計画法（MILP）によりわずか数十秒で自動作成します。スタンドアロンのデスクトップUI（Streamlit）として動作するほか、Python SDK / CLI ツールとして、また `open-hoikuict` 総合保育ICTプラットフォームの求解バックエンドとしても活用されています。

### ライセンス

**NuShift (`shiftai`) 本体**は **MIT License** のもとで公開されています。商用利用・カスタマイズを含め自由にご活用いただけます。詳細は [LICENSE](LICENSE) および [pyproject.toml](pyproject.toml) をご確認ください。（なお、`pyproject.toml` に `"Private :: Do Not Upload"` を設定しており PyPI での一般公開パッケージ配布は行わず、ソースおよびリポジトリ経由で提供しています。また連携対象の `open-hoikuict` 本体は商用プロプライエタリライセンスです。）

### 主な特長

1. **⚡ 日本初・法令配置基準連動 シフト自動作成 (NuShift Core)**
   - 厚生労働省告示第49号（全国基準: 0歳3:1, 1・2歳6:1, 3歳15:1/20:1, 4・5歳25:1/30:1）および各自治体の独自基準に完全対応。
   - 保育所における**年度基準（4月1日時点の満年齢）**による学年・配置比率の自動適用。
   - 高速オープンソースソルバー **HiGHS** および **COIN-OR CBC** による高速求解。
   - ⚠️ **適用範囲**: 本プリセットは **認可保育所（児童福祉法第32条の認可）** の配置基準に限定する。**認可外保育施設**（企業主導型・小規模・事業所内等）は配置基準・算出手法・資格要件が異なるため非対応。別途要綱に基づく検証が必要。
   - 貪欲法（Greedy）や外部初期解を用いた**ウォームスタート**により、大規模求解時間を大幅に短縮。
2. **🛡️ 違反激減エンジン ＆ 法定義務最優先最適化 (Phase A〜G 統合)**
   - **罰則序列の根本是正**: 法定休憩不足（労基法第34条）ペナルティ（6,000）＞ 配置不足（3,000）＞ 分断勤務（2,500）の序列を確立。「人員を充足させるために休憩を削る」本末転倒な挙動を撲滅。
   - **11時間ルール動的実配置二段構え検知**: 開所11時間超の園における静的在籍監査に加え、動的実配置検証（`MAIN_OPENING_COVERAGE`）により保育時間帯の常時2名以上の実配置を厳密検証。
   - **7日滑走窓による週40時間監視**: 暦週だけでなく任意の連続7日間で週40時間を厳密走査。
   - **求解前ショートフォールゲート (`shortfall_gate`)**: 「需要 ＞ 供給」で充足不可能な場合、無駄な計算を行わずに即座に停止。不足時間帯と必要追加人数を処方箋提示。
   - **違反の集約表示**: 同一職員・同一日の時間帯重複違反を1行に集約し、画面の情報過多を半減。
   - **固定長区間列挙 (`interval_candidate_limit=400`)**: 在勤1ブロック・休憩1ブロックのハード制約を安定化し、勤務分断（スプリットシフト）を完全抑止。
   - **週44時間目安の週窓判定 (R2-SOL-02修正)**: 特例措置対象事業場（週44h）の判定精度を向上。
   - **計算式・閾値の単一ソース化 (`statutory_rules.py`)**: ソルバと検査器（`gap_analysis`）の完全整合。
3. **🤝 現場運用制約（ペアリング制約・突発欠勤リペア・クラス配置）**
   - **職員ペアリング制約 (`PairingRule`)**: 「新人とベテラン」「特定資格者同士」を同一時間帯にペアで配置するルールをサポート。
   - **当日突発欠勤リアルタイムリペア (`repair_shift_absence`)**: 当日の突発的な体調不良や忌引に対し、他の確定済みシフトを極力崩さずに最短で代行配置・再最適化。
   - **クラス配置制約**: 0歳児クラス専任担当などのクラス固定・フリー保育士配分。
4. **🧩 現場定義パターンモード & 記号マトリクス整列**
   - 園固有の勤務記号（「早」「日」「遅」「夜」「A」「B」等）や勤務時間枠への吸着（`snap_to_patterns`）。
   - パターンの編集・全削除（空タプル維持）・リセットの安定動作。
   - 半端な勤務開始・終了時刻を出さず、現場掲示用記号マトリクス（Excel / CSV）へダイレクトに出力。
5. **⚖️ 労基法準拠・36協定・変形労働時間制・割増賃金シミュレーション**
   - 労働基準法第32条違反（1日10h超勤務）や法令配置割れ、11時間超開所時の常時2名割れを自動検知して承認を遮断する「BLOCKER ゲート」。
   - **36協定厳格判定**: 36協定未締結者の週40h超過・日8h超残業を違法 BLOCKER（`WEEKLY_HOURS_EXCEEDED_NO_36`, `OVERTIME_NOT_ALLOWED`）として遮断。締結済みの週40h超過は時間外労働（WARNING）として適切に分離。
   - 1か月単位の変形労働時間制（週40時間期間総枠）の適合性判定。
   - 時間外（25%）、深夜22:00〜05:00（25%・分単位高精度集計）、法定休日（35%）の割増賃金自動シミュレーション。
6. **🖥️ 直感的な Streamlit UI & 堅牢なデータ取込**
   - **データ取込安全化**: CSV取込時の文字列強制（`dtype=str`）による職員IDのゼロ落ち防止、時刻の「午後」「PM」自動変換、年齢「才」表記許容、36協定列省略時の安全側既定値（残業不可）。
   - **UI状態管理の保護**: ウィザード更新時もサイドバー詳細設定を維持し、正社員勤務時間スライダーの安全連動。
   - 専門用語を現場目線へ平易化し、アコーディオン＆ツールチップ式に再編（表示情報量を半減）。
   - ウィザード形式で園児数、職員条件、希望休をステップ順に入力可能。
   - ガントチャート表示、リアルタイムの過不足（ギャップ）可視化、手動セル編集・ピン留め（固定保護）をサポート。
   - PWA Service Worker によるオフライン耐性と高速表示。
7. **🧬 3階層スコアリング & LNS 協調最適化 (v0.8.1)**
   - **Hard/Medium/Soft 3階層スコアリング (`HardMediumSoftScore`)**: 法令違反ゼロ（Hard）、配置不足ショートフォール最小化（Medium）、職員希望・公平性・人件費最適化（Soft）を厳密な辞書式順序で評価（Timefold の制約階層設計思想を取り入れた MILP + LAHC 実装）。
   - **過剰制約計画 (Overconstrained Planning & Shortfall Capture)**: 人員不足時でも求解不能（Infeasible）に陥らず、不足コマ数（Mediumペナルティ）を最小化しながら最大限充足された安全なシフトを生成。
   - **スコア説明と制約起訴 (Score Explanation & Indictment)**: 各制約がどの職員・日付・時間帯にどれだけのペナルティを与えたかを一覧追跡（UIタブで要因別グラフィカル表示）。
   - **局所探索の多様化 (LAHC / SwapMove / ChangeMove)**: 遅延受容山登り法（Late Acceptance Hill Climbing）により、局所最適解から脱出して高品質な解を探索。
   - **差分スコア計算 & 状態追跡 (`shadow_state.py`)**: シフト変更時のインクリメンタル評価により超高速な解の改善を実現。
   - **リアルタイム最小破壊修復 (`DisruptionReport`)**: 突発欠勤リペア等において、変更人数・変更コマ数を定量レポートし現場の混乱を最小化。
   - **月間マセヒューリスティクス協調 (LNS ⇄ MIP 協調修復)**: 月間勤務パターン枠（Stage 1）と15分コマ割当（Stage 2）の大規模近傍探索（LNS）による高速協調最適化。
   - **性能ベンチマークスイート (`shiftai benchmark`)**: 7日間・30日間の実務シナリオに対する回帰防止性能ゲート（15秒以内求解）を標準装備。
8. **🚀 SaaS Micro Engine & 分散オートスケール連携 (v0.8.1 新機能)**
   - **サブセカンド即時 Re-plan (Micro Engine)**: 朝7時の突発病欠に対し、MILP起動をスキップして局所探索を直結（0.2〜0.5秒応答）し、確定シフト・ピン留めを完全保護。
   - **過剰制約自動診断サジェスト**: 不足コマの自動特定と「パート勤務延長」「希望休出勤打診」などの自然言語処方箋レポートを生成。
   - **分散ステートレスワーカー & KEDA オートスケーリング**: Web本体から重い数理最適化を完全分離し、Redisキュー長に応じた弾力的な 0〜20 Pods 自動スケール（Spot VM対応）をサポート。
9. **🦀 Rust コア (`nushift-core`) ＋ FastAPI JSON API ＋ SPA 月間シフトエディタ (Phase 2 新設)**
   - **Rust 計算・検証コア (`crates/nushift-core`)**: `statutory_rules` / ライブ検証を PyO3・WebAssembly 両対応で実装し、`rust_bridge.py` 経由で Python 環境へ透過統合（未ビルド時は純粋 Python へ安全フォールバック）。
   - **`shiftai.api` (FastAPI JSON API)**: `GET /api/v1/health`、`GET /api/v1/sample-grid`、`POST /api/v1/validate-day` およびビルド済み SPA の配信を提供。
   - **React + TypeScript 月間シフトエディタ (`frontend/`)**: 1クリック勤務記号切替（`早`/`日`/`遅`/`休`）、ピン留め、Undo、`IndexedDB` 自動永続化（リロード復元）、リアルタイム法令・実働時間チェックを実装。

---

## 2. Project A (open-hoikuict) との違い

| 項目 | Project A: open-hoikuict | Project B: NuShift (本プロジェクト) |
| :--- | :--- | :--- |
| **形態** | 総合保育ICTプラットフォーム (FastAPI Webアプリ / 単一園オールインワン公式サポート) | シフト自動作成エンジン (Streamlit デスクトップUI / MIT コアエンジン) |
| **ライセンス** | 商用プロプライエタリ (全権利保持) | **MIT License (オープンソース・PyPI非公開)** |
| **バージョン** | **v0.8.2 (open-hoikuict v0.8.2)** | **v0.8.1 (NuShift / shiftai v0.8.1)** |
| **主な用途** | 園児台帳・登降園・連絡帳・請求・シフト・監査ログ | シフト自動作成・労基法チェック・給与シミュレーション |
| **ブランド** | nusoft | nusoft (NuShift) |

両プロジェクトは「nusoft」ブランドの下にありますが、技術スタック（FastAPI vs Streamlit）とライセンス（プロプライエタリ vs MIT）が異なります。用途に応じて使い分けてください。

---

## 3. ディレクトリ構成

```text
nushift/
├── src/shiftai/            # 数理最適化シフト自動作成エンジンコア
│   ├── solver_core/        # 分割求解・MILPモデル基盤
│   ├── ui/                 # Streamlit UI (ウィザード, ガント, プレビュー, エクスポート)
│   │   ├── sidebar.py      # サイドバー・モード切替・施設設定
│   │   ├── tab_data.py     # データ投入・サンプル読込・ウィザード
│   │   ├── tab_solve.py    # 最適化実行・進捗表示・診断ログ・ショートフォールゲート
│   │   ├── tab_shift.py    # シフト表・セル編集・ピン留め・制約起訴インジケータ
│   │   └── tab_export.py   # Excel/CSV/PDF エクスポート
│   ├── benchmark.py        # ベンチマークスイート・性能評価
│   ├── compliance.py       # 労働法規・36協定・変形労働制・割増賃金計算
│   ├── config.py           # エンジン設定 (区間列挙上限, 求解タイムアウト)
│   ├── domain.py           # ドメインモデル (HardMediumSoftScore, ObjectiveWeights等)
│   ├── gap_analysis.py     # 配置割れギャップ分析・違反検知・違反集約
│   ├── greedy.py           # 高速貪欲法ソルバー・初期解生成
│   ├── local_rules.py      # 自治体別独自基準・全国基準プリセット
│   ├── local_search.py     # 局所探索 (SwapMove, ChangeMove, LAHC)
│   ├── model_builder.py    # PuLP MILP数理モデル構築
│   ├── monthly.py          # 月間シフト2段階求解・LNS協調修復
│   ├── score.py            # 3階層スコアリング & 制約起訴 (Indictment)
│   ├── shadow_state.py     # 差分スコア計算・シャドウ変数追跡
│   ├── shift_patterns.py   # 勤務パターン枠・現場用記号マトリクス定義
│   ├── solver.py           # MILP最適化モデル (HiGHS / 過剰制約計画 / 最小破壊修復)
│   ├── standards.py        # 法定配置基準エンジン (認可保育所 / 企業主導型)
│   └── statutory_rules.py  # 法定閾値・休憩計算の単一ソース定義
│   ├── solver.py           # MILP最適化モデル (HiGHS / CBC / ウォームスタート / 突発欠勤リペア)
│   ├── standards.py        # 法定配置基準エンジン (認可保育所 / 企業主導型)
│   └── statutory_rules.py  # 法定閾値・休憩計算の単一ソース定義
├── tests/                  # 単体・最適化性能・回帰テストスイート (70ファイル)
├── sample/                 # サンプルデータ (園児・職員・希望休 CSV)
├── template/               # エクセル・CSV 入力テンプレート
├── docs/                   # 仕様書・開発ガイド・データ仕様・改善計画書
├── landing_page/           # プロダクト紹介LP (HTML/CSS/JS)
├── streamlit_app.py        # Streamlit Web アプリケーションエントリーポイント
├── run_app.bat             # ワンクリック起動バッチ (Windows)
├── pyproject.toml          # パッケージ設定 & Ruff & pytest (MIT License)
├── requirements.txt        # 実行依存関係 (Streamlit, PuLP, Pandas 等)
└── LICENSE                 # MIT License
```

---

## 4. クイックスタート

### 4.1 動作要件

| 項目 | 要件 | 備考 |
| :--- | :--- | :--- |
| **OS** | Windows 11 / 10, Ubuntu 22.04 LTS / 24.04 LTS, macOS | 開発・本番両対応 |
| **Python** | 3.11 または 3.12 | 公式サポート |
| **メモリ** | 4 GB 以上推奨 | 大規模シフト求解時は 8 GB 推奨 |
| **ディスク空き容量** | 2 GB 以上 | サンプルデータ・出力ファイルを含む |
| **ソルバー** | **HiGHS（推奨, `pip install highspy`）** / COIN-OR CBC (PuLP同梱) | MILP 最適化用。30日一括モデルでは HiGHS を強く推奨（CBC は 60秒経過しても初期解が出せないことあり） |

### HiGHS 推奨と CBC の制限事項

NuShift では **HiGHS を既定ソルバーとして推奨** します。`src/shiftai/config.py` の `DEFAULT_SOLVER = "HiGHS"` で HiGHS が既定となっており、別途指定がない限り HiGHS が使用されます。

**HiGHS を推奨する理由**:
- **高速求解**: 30日一括モデル（66,396変数 / 75,358制約）で HiGHS は数秒〜数十秒で最適解を出せることが多い。
- **初期解の安定性**: CBC は大規模モデルで 60秒を経過しても初期解すら出せないことが確認されている。
- **メモリ効率**: 大規模モデルでも安定して動作し、タイムアウトのリスクが低い。

**CBC の使用について**:
COIN-OR CBC は PuLP に同梱されており、追加インストール不要です。ただし、30日一括などの大規模モデルでは解が出にくいため、小規模モデル（短期間・少人数）に限定して使用してください。大規模求解では必ず `pip install highspy` で HiGHS をインストールし、HiGHS を使用してください。

| **ブラウザ** | Chrome / Edge / Safari 最新版 | Streamlit UI 表示用 |

### 4.2 インストール手順

#### Windows (PowerShell)

```powershell
# 1. リポジトリのクローン
git clone https://github.com/hoikuict/nushift.git
cd nushift

# 2. 仮想環境の作成と有効化
python -m venv .venv
.venv\Scripts\Activate.ps1

# 3. 依存パッケージのインストール
pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt

# 4. 本パッケージのインストール（python -m shiftai / サブプロセステストに必要）
pip install -e .
```

#### Linux / macOS

```bash
# 1. リポジトリのクローン
git clone https://github.com/hoikuict/nushift.git
cd nushift

# 2. 仮想環境の作成と有効化
python -m venv .venv
source .venv/bin/activate

# 3. 依存パッケージのインストール
pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt

# 4. 本パッケージのインストール（python -m shiftai / サブプロセステストに必要）
pip install -e .
```

### 4.3 アプリケーションの起動 (Streamlit UI)

Windows 環境では `run_app.bat` をダブルクリックするか、以下のコマンドを実行します：

```bash
streamlit run streamlit_app.py
```

ブラウザで `http://localhost:8501` が開き、直感的なウィザード形式で配置基準シミュレーションやシフト作成が可能です。

### 4.4 CLI 実行

コマンドラインからバッチで解を求めることも可能です：

```bash
# サンプルデータを用いたシフト作成
python -m shiftai solve --sample --out out --time-limit 30

# HiGHS ソルバーとパターンモードを指定して実行
python -m shiftai solve --sample --solver highs --pattern-mode --out out
```

---

## 5. 品質保証・テスト・CI

本プロジェクトは厳格なテスト駆動開発とCI基準を維持しています。

```bash
# コード規約・Lint チェック
ruff check .

# フォーマット検査（CI で実行される）
ruff format --check .

# 全テストスイートの実行 (回帰テスト含む)
python -m pytest tests/ -x -q
```

CI（`.github/workflows/tests.yml`）は PR で `ruff check` / `ruff format --check` /
`pytest -m "not slow"` を実行し、main push 時に slow（CBC 統合）テストも走らせます。

すべてのテストがパスし、Ruff のチェックでエラーがゼロであることを確認してからコミットされています。

### 5.1 画面（Streamlit）の構成

NuShift は、現場の使いやすさに合わせた **シンプルモード（3 タブ）/ 上級者モード（5 タブ）** を切り替えられます。

### 5.2 ソフトペナルティと制約区分

**ソフトペナルティ**（重みで調整します。違反しても解は出ますが、検査で警告します）:

- 連続勤務日数 / 週の最大出勤日数
- 最低休憩時間 / 勤務間の休息時間
- 早朝・延長の回避 / 勤務時間の偏り / 月間時間
- 未使用職員 / 勤務希望

※ `payroll.csv` の `実働時間` は `総勤務時間` と同じ値です。**休憩を二重控除しません**。

### 5.3 Python API クイック実行デモ

```python
import datetime as dt

from shiftai import local_rules, solver, standards
from shiftai.domain import (
    AgeClass,
    ChildPlan,
    Contract,
    EmploymentType,
    Role,
    StaffMember,
    StaffPreferences,
)
from shiftai.gap_analysis import check_violations

DAY = dt.date(2026, 10, 2)  # 金
DAY_OPEN, DAY_CLOSE, GRAN = dt.time(9, 0), dt.time(15, 0), 30

# --- 園児 5 名が 9:00-15:00 在園 ---------------------------------------
children = [
    ChildPlan(
        f"C{i:03d}",
        f"園児{i}",
        DAY,
        AgeClass.AGE_3,
        arrive=DAY_OPEN,
        depart=DAY_CLOSE,
        is_short_time=False,
        absent=False,
        absent_reason="",
        uses_early_care=False,
        uses_late_care=False,
        notes="",
    )
    for i in range(1, 6)
]

# --- 配置基準のプリセットを選ぶ ------------------------------------------
standard = local_rules.get_standard("全国基準（厚労省）")

# --- 「何人必要か」とその根拠を時間帯ごとに算出 --------------------------
table = standards.build_requirements(
    children,
    [DAY],
    standard,
    day_open=DAY_OPEN,
    day_close=DAY_CLOSE,
    granularity_min=GRAN,
)
print(f"必要人員: {len(table.slots)} 時間帯 / {sum(len(v) for v in table.rows.values())} 要件行")
for row in table.rows[DAY][:3]:
    print(
        f"  {row.slot.start:%H:%M}-{row.slot.end:%H:%M}  "
        f"必要 {row.needed_staff} 名（保育士 {row.needed_qualified} 名）"
    )
print(f"  根拠: {table.rows[DAY][0].basis}")

# --- 職員を用意して解を求める --------------------------------------------
contract = Contract(
    weekly_hours=40.0,
    daily_hours=8.0,
    employment_type=EmploymentType.SEI,
    min_monthly_hours=150.0,
    max_monthly_hours=185.0,
    max_weekly_days=5,
    max_consecutive_days=5,
    min_rest_hours=11.0,
    granularity_min=GRAN,
    earliest_start=dt.time(8, 0),
    latest_end=dt.time(17, 0),
)
staff = [
    StaffMember(f"S{i:02d}", name, (Role.HOIKUSHI,), contract)
    for i, name in enumerate(["井上", "小林", "斎藤", "松本"], 1)
]
prefs = {s.staff_id: StaffPreferences() for s in staff}

result = solver.solve_shift(children, staff, table, prefs, time_limit_sec=30, standard=standard)
print(f"\n解の状態: {result.status.value}  目的関数={result.objective_value}")

# --- 結果を見る ---------------------------------------------------------
shift_day = result.day(DAY)
required = {r.slot.label: r.needed_staff for r in table.rows[DAY]}
for slot in table.slots:
    worked = [s.staff_id for s in staff if shift_day.get(s.staff_id, slot).name == "WORK"]
    who = " ".join(worked) if worked else "(全員休息)"
    print(
        f"  {slot.start:%H:%M}-{slot.end:%H:%M}  "
        f"必要 {required[slot.label]} 名 → {len(worked)} 名  {who}"
    )

violations = check_violations(table, result, staff, prefs, standard=standard)
blocking = [v for v in violations if v.severity.name == "BLOCKER"]
print(f"\n違反: BLOCKER {len(blocking)} 件 / 全体 {len(violations)} 件")

names = {s.staff_id: s.name for s in staff}
for s in staff:
    hours = sum(0.5 for slot in table.slots if shift_day.get(s.staff_id, slot).name == "WORK")
    print(f"  {s.staff_id} {names[s.staff_id]}: {hours:.1f} 時間（{int(hours * 60)} 分）")
```

#### 実行出力例

```text
必要人員: 12 時間帯 / 12 要件行
  09:00-09:30  必要 2 名（保育士 1 名）
  09:30-10:00  必要 2 名（保育士 1 名）
  10:00-10:30  必要 2 名（保育士 1 名）
  根拠: 3歳児 5名 ÷ 8 = 1名（定員比8:1、切り上げ）／保育標準時間／2名ルールによる底上げ

解の状態: 最適解  目的関数=16.0
  09:00-09:30  必要 2 名 → 4 名  S01 S02 S03 S04
  09:30-10:00  必要 2 名 → 4 名  S01 S02 S03 S04
  10:00-10:30  必要 2 名 → 3 名  S01 S03 S04
  ...
違反: BLOCKER 0 件 / 全体 0 件
  S01 井上: 5.0 時間（300 分）
  S02 小林: 5.0 時間（300 分）
  S03 斎藤: 5.0 時間（300 分）
  S04 松本: 5.0 時間（300 分）
```

---

## 6. サポート対象ブラウザ・デバイス

| 端末区分 | 推奨環境 | 主な用途 |
| :--- | :--- | :--- |
| **事務用 PC** | Windows 10/11, macOS, Chrome / Edge 最新版 | 園長・主任・事務の管理業務、シフト作成 |
| **保育室 タブレット** | iPad (iPadOS 16+), Android タブレット (10インチ以上) | シフト表確認、セル編集 |
| **保護者 スマートフォン** | iPhone (iOS 15+), Android (Android 10+) | 出勤シフト確認 (iCal連携) |

---

## 7. ドキュメント一覧

- 📖 **[MANUAL.md (総合操作・運用マニュアル)](MANUAL.md)**: 園長・保育士・事務・管理者向けの実務的利用手順書
- ❓ **[Q_AND_A.md (よくある質問と回答)](Q_AND_A.md)**: 現場の疑問、ソルバーの仕組み、トラブルシューティング集
- 🎨 **[ランディングページ (landing_page/index.html)](landing_page/index.html)**: NuShift の魅力を伝える美麗なプロダクト紹介LP
- 🛠️ **[docs/05_開発ガイド.md](docs/05_開発ガイド.md)**: アーキテクチャ設計・CLI仕様・開発者向けガイド
- 📋 **[docs/04_データ仕様.md](docs/04_データ仕様.md)**: 入出力データ仕様・CSV/Excelフォーマット定義

---

## 8. ライセンス

NuShift コアエンジンおよび Streamlit アプリケーションは **MIT License** のもとで公開されています。商用利用・カスタマイズを含め自由にご活用いただけます。詳細は [LICENSE](LICENSE) をご覧ください。
