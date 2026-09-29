# 🧸 配置基準連動型 シフト自動作成AI

園児の登降園予定と保育所の**職員配置基準**から、必要な人員を時間帯ごとに算出し、
労働基準法と職員ごとの契約・希望を守ったシフトを MILP（PuLP/CBC）で自動作成する
Streamlit アプリです。ブラウザで操作する GUI と、ブラウザ無しで完結する CLI の両方を用意しています。

- 必要人員の**根拠（basis）**を全セルに持たせ、UI で「なぜこの人数が必要か」を説明
  （`Requirement.basis` をそのまま説明文として表示）
- 2名ルール・延長保育の代替措置・短時間保育 ONLY 園など、自治体ごとのプリセットに対応
- 労働基準法（休憩 45/60 分・休息 11 時間・週 44 時間・月 45 時間）と
  契約（週契約時間・1日上限・週最大出勤日数・連続勤務上限）を**ハード制約**として扱う
- 希望休・出勤不可はハード制約、稼働時間の偏りはソフトペナルティで調整

---

## 1. 必要環境

| 項目 | 値 |
| --- | --- |
| Python | 3.11 以上（3.12 でも動作） |
| 主な依存 | `streamlit` / `pandas` / `numpy` / `pulp` / `openpyxl` |

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -e .          # 開発用（pytest 等の dev 依存は -e ".[dev]"）
```

<details>
<summary>別のインストール経路（<code>requirements.txt</code> / <code>Makefile</code>）</summary>

上の `pip install -e .` が正規手順です。以下は**同じ結果になる代替経路**です。

```bash
# --- 代替 A: requirements から解決する ---
python -m pip install -r requirements.txt        # 実行時依存（pyproject.toml の写し）
python -m pip install -r requirements-dev.txt    # 開発・テスト用（pytest / ruff / xdist など）

# --- 代替 B: Makefile に任せる ---
make install-dev     # venv を作り、実行時 + 開発用依存を入れる
make help            # 全ターゲットの一覧（既定ターゲット）
```

`requirements.txt` は `pyproject.toml` の `[project].dependencies` を写した運用用の写しで、
**唯一の真実（single source of truth）は `pyproject.toml`** です。両者は必ず同時に直します。
`Makefile` のターゲットは Windows ネイティブに `make` が無い環境では使えません（WSL / Git Bash を使うか、
`docs/05_開発ガイド.md` の対応表に書かれたコマンドを直接叩いてください）。

</details>

## 2. 起動

```bash
shiftai ui                         # コンソールスクリプト（既定 8501 ポート）
shiftai ui --port 8600 --headless  # ポート変更 + ブラウザを開かない

streamlit run streamlit_app.py     # 従来どおり直接起動する場合
```

ブラウザで <http://localhost:8501> を開きます。**3 ステップ**で使えます。

1. **タブ1 データ投入** — CSV / Excel / JSON をアップロード、または「サンプルデータで試す」
2. **タブ2 必要人員** — 配置基準と開所時間を選び、時間帯ごとの必要人員と根拠を確認
3. **タブ3 シフト作成** — 「シフトを自動作成する」を押す → タブ4 で微調整 → タブ5 で出力

## 3. 入力データ

3 つの表を用意します。**UTF-8（推奨 utf-8-sig）／cp932 の CSV**、`.xlsx`、`.json` に対応しています。
列名は日本語の別名も認識します（例: `staff_id` / `職員ID`、`開始` / `start`）。

`shiftai template --out template` で **各 1 行入りの空テンプレート**が出力されます。

### 3-1. 園児（登降園予定） `children.csv`

| 列 | 必須 | 説明 |
| --- | --- | --- |
| 園児ID | ○ | 一意な ID（例: `C001`） |
| 氏名 | | 表示用 |
| 年齢 | ○ | `0`〜`5`（年齢クラス） |
| 登園日 | ○ | `YYYY-MM-DD` |
| 登園時刻 | ○ | `HH:MM` |
| 降園時刻 | ○ | `HH:MM`（登園より後） |
| 短時間保育 | | `true` / `false`。保育標準時間帯の在園者数にだけ数える |
| 欠席 / 欠席理由 | | `true` の日は在園者数に含めない |
| 早朝保育 / 延長保育 | | `true` で該当時間帯の在園者数を反映 |
| 備考 | | 自由記入 |

### 3-2. 職員 `staff.csv`

| 列 | 必須 | 説明 |
| --- | --- | --- |
| 職員ID | ○ | 一意な ID（例: `S001`） |
| 氏名 | | 表示用 |
| 資格（主） | ○ | 保育士 / 子育て支援員 / 幼稚園教諭 / 看護師 / 栄養教諭 / 調理員 / 薬剤師 / 園長・主任（配置対象外） |
| 資格（副） | | 複数資格は `\|` 区切り |
| 雇用形態 | | 正職員 / パート / 契約社員 / アルバイト |
| 週契約時間 / 1日契約時間 | | 例: `40` / `8` |
| 月間最小時間 / 月間最大時間 | | 省略時は週契約時間から自動計算 |
| 週最大出勤日数 / 最大連続勤務日数 | | 例: `5` / `5` |
| 最早始業 / 最遅終業 | | 契約上就業できる時間帯（`07:00` / `20:00`） |
| 能力タグ / 備考 | | 自由記入 |

### 3-3. 希望休・出勤不可 `preferences.csv`（任意）

| 列 | 必須 | 説明 |
| --- | --- | --- |
| 職員ID | ○ | 職員CSVと一致 |
| 種別 | ○ | `希望休` / `出勤不可`（ハード制約）、`休み希望` / `出勤希望`（ソフト） |
| 日付 | ○ | `YYYY-MM-DD` |
| 開始 / 終了 | | 時間帯を指定する場合は `09:00` / `12:00`、空欄なら終日 |
| 理由 | | 表示用 |

読み込めない行は全体を落とさず **issues として積み上げて表示**されます（`LoadResult.issues`）。

## 4. 配置基準のプリセット

```bash
shiftai presets           # 一覧を表示
shiftai presets --json    # JSON で取得
```

全国基準（厚労省）、東京都、横浜市、大阪市、福岡市、名古屋市、京都市、札幌市、神戸市、川崎市、
保育標準時間のみ園 の 11 種類です。各プリセットには出典（告示名）を設定しています。

> **注意**: プリセットの数値は一般的な実務水準に基づく目安です。
> 実際の運用では必ず自治体の告示・条例・要綱への個別確認を行ってください。

## 5. CLI（ヘッドレス実行）

```bash
# 動作確認（サンプルデータで 1 週間分を自動作成）
shiftai solve --sample --out out --time-limit 60 --zip

# 実データで実行
shiftai solve \
  --children  data/children.csv \
  --staff     data/staff.csv \
  --preferences data/preferences.csv \
  --standard  福岡市 \
  --start 2026-10-01 --end 2026-10-07 \
  --open 09:00 --close 14:00 --granularity 30 \
  --out out --zip --strict
```

主なオプション:

| オプション | 説明 |
| --- | --- |
| `--children` / `--staff` / `--preferences` | 入力ファイル（CSV / Excel / JSON） |
| `--standard` | 配置基準プリセット名（既定: 全国基準（厚労省）） |
| `--start` / `--end` | 対象期間。省略時は園児データの日付範囲 |
| `--open` / `--close` / `--granularity` | 開所・閉所時刻と時間帯の粒度（分） |
| `--closed` / `--holiday` | 休園日 / 祝日・行事日（1 つにつき 1 回指定） |
| `--no-min-two` | 2名ルールの底上げを無効化 |
| `--time-limit` | ソルバの上限秒数（既定 60） |
| `--zip` | 成果物 ZIP も出力 |
| `--strict` | 要調整（WARNING）があれば終了コード 3 |

終了コード:

| コード | 意味 |
| --- | --- |
| 0 | 正常終了（法令違反なし） |
| 1 | 入力・実行エラー |
| 2 | 法令違反（BLOCKER）が 1 件以上 |
| 3 | `--strict` 指定時かつ要調整（WARNING）が 1 件以上 |

## 6. 出力物

`shiftai solve` は `--out` ディレクトリに次を書き出します。

| ファイル | 内容 |
| --- | --- |
| `shift.csv` | 職員×日×時間帯の長い形式（勤務 / 休憩 / オフ） |
| `shift_matrix.csv` | 全日を 1 枚にまとめた勤務表 |
| `payroll.csv` | 職員ごとの勤務時間・休憩・早朝/延長回数・推定人件費 |
| `requirements.csv` | 時間帯ごとの必要人員と**算出根拠** |
| `gap.csv` / `gap_daily.csv` | 必要人員と実際の配置の過不足（日別） |
| `violations.csv` | 法令違反・要調整の一覧（該当時のみ） |
| `shift.ics` | 職員 1 人 1 勤務 = 1 イベントの iCalendar |
| `summary.md` | 配置基準適合状況のサマリー |
| `bundle.zip` | 上記 + Excel（`--zip` 指定時） |

## 7. Google Apps Script 連携

Google シート（園児登降園予定 / 職員 / シフト）を直接読み書きする GAS Web App を同梱しています。
手順は **[`gas/README.md`](gas/README.md)** を参照してください（コピペで再現できる粒度で記述）。

環境変数 `SHIFTAI_GAS_URL` / `SHIFTAI_GAS_SHEET` / `SHIFTAI_GAS_SECRET` / `SHIFTAI_GAS_TIMEOUT`
を設定すると UI の連携セクションが有効になります。未設定なら例外は出ません。

## 8. 開発

```bash
python -m pip install -e ".[dev]"
python -m pytest                 # 全テスト
python -m pytest -m "not slow"   # 重い統合テストを除く（1〜2 分）
python -m pytest --cov=shiftai   # カバレッジ
ruff check .                     # lint（line-length 100 / E,F,I,UP,B）
```

同じことが `make` でもできます（`make help` で全ターゲットが列挙されます）。

```bash
make run          # = python -m streamlit run streamlit_app.py
make test-fast    # = python -m pytest -m "not slow"
make lint         # = python -m ruff check .
make solve        # サンプルデータで 1 週間分をヘッドレス実行して out/ に出力
```

詳細は [`docs/05_開発ガイド.md`](docs/05_開発ガイド.md) を参照してください。

### モジュール構成

| モジュール | 責務 |
| --- | --- |
| `shiftai.domain` | 全モジュールが依存する共通型（変更はここから） |
| `shiftai.data_loader` | CSV / Excel / JSON の読込と検証 |
| `shiftai.standards` | 配置基準エンジン（何人が必要か） |
| `shiftai.solver` | MILP モデルと解のデコード（誰がいつ働くか） |
| `shiftai.gap_analysis` | 過不足・法令違反の検査とレポート |
| `shiftai.exporter` | CSV / Excel / ICS / 給与計算 CSV / ZIP 出力 |
| `shiftai.local_rules` | 自治体別プリセットと運用メモ |
| `shiftai.sample_data` | 決定論的なサンプルデータ生成 |
| `shiftai.gas_client` | GAS Web App クライアント（標準ライブラリのみ） |
| `shiftai.ui` | Streamlit のタブ・サイドバー・テーマ |
| `shiftai.__main__` | CLI（`ui` / `presets` / `sample` / `template` / `solve`） |

「必要人員を算出する工程（`standards`）」と「シフトを最適化する工程（`solver`）」は
分断されています。`solver` は `RequirementTable` だけを受け取るため、
基準を差し替えてもソルバ側は触る必要がありません。

## 9. 詳細ドキュメント

この README は Getting Started 用です。内部仕様・設計判断・運用手順は `docs/` に分けてあります。
**すでに内部仕様とフィールド名まで追っている場合**、そちらのほうが情報量が多く早いです。

| ドキュメント | 内容 |
| --- | --- |
| [`docs/01_アーキテクチャ.md`](docs/01_アーキテクチャ.md) | レイヤー構成と依存の向き、循環回避の方法、`standards` / `solver` 分断の理由、`domain` 凍結契約の変更手順 |
| [`docs/02_ソルバ仕様.md`](docs/02_ソルバ仕様.md) | MILP の決定変数 26 種、ハード / ソフト制約の一覧、目的関数、2 パス構成、貪欲法フォールバック、実行時間の実測値、違反コード 21 種 |
| [`docs/03_配置基準と自治体ルール.md`](docs/03_配置基準と自治体ルール.md) | 定員比 → 切り上げ → 2名ルール、`StaffingStandard` の全フィールド、時間帯の種別判定、自治体プリセット 11 種の数値と出典 |
| [`docs/04_データ仕様.md`](docs/04_データ仕様.md) | 入力 3 表の全列と受理形式、`parse_*` の実測、ヘッダゆれの吸収、`LoadIssue` / `LoadResult`、出力 10 種の形式、GAS シート構成 |
| [`docs/05_開発ガイド.md`](docs/05_開発ガイド.md) | 環境構築、`Makefile` の全 17 ターゲットと対応コマンド、テストスイートの構成と `conftest.py` のフィクスチャ、lint 設定、コード規約 |
| [`docs/06_運用手引き.md`](docs/06_運用手引き.md) | 日次運用フロー、法令違反を出さないための運用ルール、早出 / 延長 / 連続勤務の扱い、的前提条件、障害の切り分け |

GAS（Google Apps Script）連携のセットアップ手順は [`gas/README.md`](gas/README.md) にあります。

## 10. 免責

本ソフトウェアは配置基準の**目安**をLP/MILP で高速計算するための補助ツールです。
生成されたシフトは 반드시**園の責任者が法令・条例・契約と実際の勤務状況に照らして確認**してください。
