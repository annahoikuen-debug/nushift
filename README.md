# ⚡ NuShift (shiftai v0.4.1)

> **保育のすべてをつなぎ、先生の時間をこどもたちへ**  
> 職員配置基準連動型 数理最適化シフト自動作成エンジン（NuShift）と、直感的な Streamlit UI による保育園・こども園向け勤務シフト自動生成システム。

[![Version](https://img.shields.io/badge/version-v0.4.1-blue.svg)](pyproject.toml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](pyproject.toml)
[![Solver: HiGHS & CBC](https://img.shields.io/badge/Solver-HiGHS%20%7C%20CBC-orange.svg)](src/shiftai/solver.py)
[![CI](https://img.shields.io/badge/CI-Tests%20%7C%20Lint-blueviolet.svg)](.github/)
[![Support](https://img.shields.io/badge/Support-Email%20%7C%20Issues-7b1fa2.svg)](SUPPORT.md)
[![Community](https://img.shields.io/badge/Community-Discussions-9cf.svg)](https://github.com/hoikuict/nushift/discussions)

**[操作マニュアル](MANUAL.md) | [Q&A (よくある質問)](Q_AND_A.md) | [ランディングページ](landing_page/index.html) | [開発ガイド](docs/05_開発ガイド.md) | [データ仕様](docs/04_データ仕様.md)**

---

## 1. NuShift (shiftai) とは

**NuShift** は、厚生労働省の保育所等職員配置基準および各自治体（東京都・福岡市等）の独自基準・加算基準に完全対応した、**職員配置基準連動型の数理最適化シフト自動作成エンジン**です。

現場の主任保育士が毎月数日〜1週間かけて作成・微調整していた複雑な勤務シフト表を、混合整数線形計画法（MILP）によりわずか数十秒で自動作成します。スタンドアロンのデスクトップUI（Streamlit）として動作するほか、Python SDK / CLI ツールとして、また `open-hoikuict` 総合保育ICTプラットフォームの求解バックエンドとしても活用されています。

### ライセンス

本プロジェクトは **MIT License** のもとで公開されています。商用利用・カスタマイズを含め自由にご活用いただけます。詳細は [LICENSE](LICENSE) および [pyproject.toml](pyproject.toml) をご確認ください。

### 主な特長

1. **⚡ 日本初・法令配置基準連動 シフト自動作成 (NuShift Core)**
   - 厚生労働省告示第49号（全国基準: 0歳3:1, 1・2歳6:1, 3歳15:1/20:1, 4・5歳25:1/30:1）および各自治体の独自基準に完全対応。
   - 保育所における**年度基準（4月1日時点の満年齢）**による学年・配置比率の自動適用。
   - 高速オープンソースソルバー **HiGHS** および **COIN-OR CBC** による高速求解。
   - 貪欲法（Greedy）や外部初期解を用いた**ウォームスタート**により、大規模求解時間を大幅に短縮。
2. **🧩 現場定義パターンモード & 記号マトリクス整列**
   - 園固有の勤務記号（「早」「日」「遅」「夜」「A」「B」等）や勤務時間枠への吸着（`snap_to_patterns`）。
   - 半端な勤務開始・終了時刻を出さず、現場掲示用記号マトリクス（Excel / CSV）へダイレクトに出力。
3. **⚖️ 労基法準拠・36協定・変形労働時間制・割増賃金シミュレーション**
   - 労働基準法第32条違反（1日10h超勤務）や法令配置割れを自動検知して承認を遮断する「BLOCKER ゲート」。
   - 36協定未締結者の8時間超勤務をハード制約で禁止。
   - 1か月単位の変形労働時間制（週40時間期間総枠）の適合性判定。
   - 時間外（25%）、深夜（25%）、法定休日（35%）の割増賃金自動シミュレーション。
4. **🖥️ 直感的な Streamlit UI（初心者向け 3タブ / 上級者向け 5タブ）**
   - ウィザード形式で園児数、職員条件、希望休をステップ順に入力可能。
   - ガントチャート表示、リアルタイムの過不足（ギャップ）可視化、手動セル編集・ピン留め（固定保護）をサポート。

---

## 2. Project A (open-hoikuict) との違い

| 項目 | Project A: open-hoikuict | Project B: NuShift (本プロジェクト) |
| :--- | :--- | :--- |
| **形態** | 総合保育ICTプラットフォーム (FastAPI Webアプリ) | シフト自動作成エンジン (Streamlit デスクトップUI) |
| **ライセンス** | 商用プロプライエタリ (全権利保持) | **MIT License (オープンソース)** |
| **バージョン** | v0.4.0 | v0.4.1 |
| **主な用途** | 園児台帳・登降園・連絡帳・請求・シフト・監査ログ | シフト自動作成・労基法チェック・給与シミュレーション |
| **ブランド** | nusoft | nusoft (NuShift) |

両プロジェクトは「nusoft」ブランドの下にありますが、技術スタック（FastAPI vs Streamlit）とライセンス（プロプライエタリ vs MIT）が異なります。用途に応じて使い分けてください。

---

## 3. ディレクトリ構成

```text
nushift/
├── src/shiftai/            # 数理最適化シフト自動作成エンジンコア
│   ├── ui/                 # Streamlit UI (ウィザード, ガント, プレビュー, エクスポート)
│   │   ├── sidebar.py      # サイドバー・モード切替・施設設定
│   │   ├── tab_data.py     # データ投入・サンプル読込・ウィザード
│   │   ├── tab_solve.py    # 最適化実行・進捗表示・診断ログ
│   │   ├── tab_shift.py    # シフト表・セル編集・ピン留め
│   │   └── tab_export.py   # Excel/CSV/PDF エクスポート
│   ├── compliance.py       # 労働法規・36協定・変形労働制・割増賃金計算
│   ├── domain.py           # ドメインモデル (AgeClass, ChildPlan, StaffMember, Slot, etc.)
│   ├── gap_analysis.py     # 配置割れギャップ分析・違反検知
│   ├── local_rules.py      # 自治体別独自基準・全国基準プリセット
│   ├── shift_patterns.py   # 勤務パターン枠・現場用記号マトリクス定義
│   ├── solver.py           # MILP最適化モデル (HiGHS / CBC / ウォームスタート)
│   └── standards.py        # 法定配置基準エンジン (認可保育所 / 企業主導型)
├── tests/                  # 単体・最適化性能・回帰テストスイート (53ファイル)
├── sample/                 # サンプルデータ (園児・職員・希望休 CSV)
├── template/               # エクセル・CSV 入力テンプレート
├── docs/                   # 仕様書・開発ガイド・データ仕様
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
| **ソルバー** | HiGHS (推奨, `pip install highspy`) / COIN-OR CBC (PuLP同梱) | MILP 最適化用 |
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

# 全テストスイートの実行 (回帰テスト含む)
python -m pytest tests/ -x -q

# ドキュメントのビルド検証
python -m mkdocs build --strict
```

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

- 📖 **[MANUAL.md (総合操作・運用マニュアル)](MANUAL.md)**: 園長・保育士・事務・管理者向けの実践的利用手順書
- ❓ **[Q_AND_A.md (よくある質問と回答)](Q_AND_A.md)**: 現場の疑問、ソルバーの仕組み、トラブルシューティング集
- 🎨 **[ランディングページ (landing_page/index.html)](landing_page/index.html)**: NuShift の魅力を伝える美麗なプロダクト紹介LP
- 🛠️ **[docs/05_開発ガイド.md](docs/05_開発ガイド.md)**: アーキテクチャ設計・CLI仕様・開発者向けガイド
- 📋 **[docs/04_データ仕様.md](docs/04_データ仕様.md)**: 入出力データ仕様・CSV/Excelフォーマット定義

---

## 8. ライセンス

NuShift コアエンジンおよび Streamlit アプリケーションは **MIT License** のもとで公開されています。商用利用・カスタマイズを含め自由にご活用いただけます。詳細は [LICENSE](LICENSE) をご覧ください。
