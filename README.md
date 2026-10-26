# 🧸 nusoft (open-hoikuict v0.4.0)

> **保育のすべてをつなぎ、先生の時間をこどもたちへ**  
> 職員配置基準連動型 数理最適化シフト自動作成エンジン（NuShift）と、保育園・こども園の日常業務をワンストップで支える次世代オープンソース保育ICTプラットフォーム。

[![Version](https://img.shields.io/badge/version-v0.4.0-blue.svg)](version.py)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](pyproject.toml)
[![Security: MFA & Audit](https://img.shields.io/badge/Security-MFA%20%7C%20Audit%20%7C%20RateLimit-orange.svg)](security_config.py)

**[デモ環境](https://demo.hoikuict.net/) | [本番環境](https://open.hoikuict.net/) | [操作マニュアル](MANUAL.md) | [Q&A (よくある質問)](Q_AND_A.md) | [ランディングページ](landing_page/index.html) | [技術ガイド](docs/technical-guide.md)**

---

## 1. nusoft とは

**nusoft** は、保育総合ICTシステム `open-hoikuict` を中核とし、職員配置基準連動型 数理最適化ソルバー **NuShift (shiftai)**、多要素認証 (MFA)、全銀Zenginフォーマット請求連携、および堅牢なエンタープライズ・セキュリティ基盤を完全統合したオープンソース保育ICTプラットフォームです。

### 主な特長

1. **🧸 保育業務のワンストップ統合**
   - 園児・保護者台帳、登降園キオスク（タッチ打刻）、体温・機嫌・食事等の日次連絡帳、独自項目UUID対応の児童観察記録・指導要録、アレルギー・身体測定を含む健康管理、Web Push通知まで全業務を網羅。
2. **⚡ 日本初・法令配置基準連動 シフト自動作成 (NuShift Core)**
   - 厚生労働省告示第49号（全国基準）および各自治体の独自基準（東京都、福岡市など）に完全対応。
   - 混合整数線形計画法（MILP / CBCソルバー）により、週7日間の勤務割を数秒〜数十秒で自動作成。
   - 現場の微調整を保護する「セル編集・ピン留め（Pinning）」機能、労働基準法第32条（1日10h超）違反や配置不足を自動検出して承認をブロックする「BLOCKER 承認ゲート」、急なお迎え予定変更にリアルタイム連動する「配置割れギャップ検知」を搭載。
3. **💰 延長保育料自動計算 & 全銀Zengin口座振替**
   - 登降園実績に基づく延長料金の自動算出。
   - 全銀協標準フォーマット（Zengin）準拠の口座振替データを1クリックで生成し、金融機関とのスムーズな振替連携を実現。
4. **🔒 金融・医療水準のゼロトラストセキュリティ**
   - メールOTPによる多要素認証 (MFA) 標準対応（TOTP専用アプリ不要）。
   - レートリミット（IP偽装防止・メモリ自動クリーンアップ機能付き）。
   - 児童・保護者・職員の機微情報アクセスを記録する監査ログ基盤。
   - 厳格なセキュリティヘッダー（CSP, HSTS, X-Frame-Options, CSRF保護）と楽観的排他ロック (`lock_version`)。

---

## 2. システム構成と最新ファイル状態 (v0.4.0)

本リポジトリは **v0.4.0** として、全てのフェーズ（Phase 0〜3）の負債解消と機能統合が完了しています。

| 指標 | 実績値 | 備考 |
| :--- | :--- | :--- |
| **Python ファイル数** | 316 本 | vendor, venv, gen_bunnrei を除く |
| **Python 総コード行数** | 約 85,200 行 | 型アノテーション・Ruff準拠 |
| **データモデル (`models.py`)** | 175 クラス / 2,688 行 | SQLModel / SQLite & PostgreSQL 両対応 |
| **APIルーター (`routers/`)** | 38 ファイル (全45ルーター) | FastAPI 構成 |
| **テストスイート** | 100 ファイル以上 | pytest / 回帰防止テスト完備 |
| **シフトソルバー統合** | `vendor/nushift/shiftai` | CBCソルバー非同期ワーカー駆動 |

### ディレクトリ構成

```text
open-hoikuict/
├── child_records/          # 児童観察記録・要録・成長進捗管理 (独自項目UUID対応)
├── common/                 # 共通楽観ロック (optimistic_lock.py) など基盤モジュール
├── data/                   # 規定データ・初期マスタ
├── docs/                   # 仕様書・運用設計書・導入ガイド・技術ドキュメント
├── landing_page/           # 【新規】モダンで美しいプロダクト紹介LP (HTML/CSS/JS)
├── plan_docs/              # 指導計画・月案・文例ライブラリ
├── routers/                # FastAPI ルーター群 (登降園、請求、MFA、職員、児童など)
├── shift_schedule/         # 職員シフト自動作成 UI・API・求解ワーカー・ギャップ検知
│   ├── routers/            # シフト計画、セル編集、エクスポート、施設設定
│   └── services/           # solve_jobs (CBC非同期ワーカー), gap_analysis
├── static/                 # 静的アセット (CSS, JavaScript, アイコン, フォント)
├── templates/              # Jinja2 HTML テンプレート群
├── tests/                  # 単体・結合・セキュリティ回帰テストスイート
├── vendor/nushift/shiftai/ # NuShift ソルバーコア (MILP / 配置基準最適化エンジン)
├── auth.py / local_auth.py # 認証基盤 (Argon2id, セッション管理)
├── audit_logger.py         # 機微情報アクセス監査ログ
├── database.py             # データベース接続・マイグレーション・テーブル初期化
├── main.py                 # FastAPI アプリケーションエントリーポイント
├── MANUAL.md               # 【新規】包括的ユーザー・運用操作マニュアル
├── pyproject.toml          # プロジェクト設定・Ruff・pytest 設定
├── Q_AND_A.md              # 【新規】保育現場・管理者向け総合 Q&A / FAQ
├── rate_limiter.py         # レート制限 (メモリ掃除・IP偽装対策)
├── requirements.txt        # 本番実行依存関係 (FastAPI, PuLP, Pandas 等)
├── security_config.py      # セキュリティ設定・環境バリデーション
├── security_headers.py     # セキュリティHTTPレスポンスヘッダー
├── version.py              # バージョン定義 (v0.4.0)
└── zengin_service.py       # 全銀協標準フォーマット口座振替サービス
```

---

## 3. クイックスタート

### 3.1 動作要件

- **OS**: Windows 11 / 10, Ubuntu 22.04 LTS / 24.04 LTS, macOS, TrueNAS SCALE
- **Python**: 3.11 または 3.12
- **ソルバー**: COIN-OR CBC ソルバー (PuLP 経由で同梱、またはシステムインストール)

### 3.2 インストール手順

```bash
# 1. リポジトリのクローン
git clone https://github.com/hoikuict/open-hoikuict.git
cd open-hoikuict

# 2. 仮想環境の作成と有効化
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# 3. 依存パッケージのインストール
pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt

# 4. 環境変数の設定 (開発環境用サンプルのコピー)
cp .env.example .env
```

### 3.3 アプリケーションの起動

```bash
# 開発サーバー起動
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

起動後、ブラウザで以下のURLにアクセスしてください：
- **メインアプリケーション**: `http://127.0.0.1:8000/`
- **ランディングページ**: `http://127.0.0.1:8000/landing_page/` (または `landing_page/index.html` を直接ブラウザで開く)
- **API ドキュメント (Swagger UI)**: `http://127.0.0.1:8000/docs`

### 3.4 シフト自動作成単体エンジン (NuShift / Streamlit) の起動

NuShift コアエンジン単体をデスクトップUIで操作する場合：

```bash
# nushift ディレクトリへ移動 (別リポジトリまたは vendor)
cd vendor/nushift/shiftai  # または e:\nushift\nushift
python -m streamlit run streamlit_app.py
```

ブラウザで `http://localhost:8501` が開き、直感的なウィザード形式で配置基準シミュレーションやシフト作成が可能です。

---

## 4. 主要機能詳細

### 4.1 職員配置基準連動 シフト自動作成 (NuShift Core)

| 機能 | 詳細 |
| :--- | :--- |
| **基準比率設定** | 0歳児 (3:1), 1・2歳児 (6:1), 3歳児 (15:1 または 20:1), 4・5歳児 (25:1 または 30:1)。厚労省全国基準および自治体加算に対応。 |
| **数理最適化 (MILP)** | CBC ソルバーが、職員の雇用契約（常勤・非常勤）、希望休、有給休暇、連続勤務上限、休憩時間（法定45分/60分）を満たす最適シフトを週単位で算出。 |
| **手動編集 & ピン留め** | 作成されたシフト表のセルを画面上で手動変更可能。特定セルを「ピン留め」すると、再計算時にもその配置が固定保護されます。 |
| **BLOCKER 承認ゲート** | 労働基準法第32条違反（1日10時間超勤務）や法令配置割れが存在する場合、園長承認が HTTP 409 で自動遮断されます。 |
| **登降園ギャップ検知** | 保護者のお迎え予定変更や突発的な延長保育に対し、当日の必要保育士数と現行シフトの過不足（ギャップ）を即座に検知・警告。 |
| **マルチエクスポート** | 施設掲示用の「Excel 勤務割表」、給与連携用の「BOM付き CSV」、スマホカレンダー連携用の「iCalendar (.ics)」を出力。 |

### 4.2 登降園・保護者ポータル・Web Push・動画配信

- **キオスク端末打刻**: タブレットやChromebookで、園児・保護者用QR/タッチ打刻をサポート。
- **保護者ポータル**: スマホから当日の出欠連絡、お迎え時間変更、連絡帳の記入・閲覧が可能。
- **Web Push 通知**: ブラウザ標準のプッシュ通知技術により、アプリのインストール不要で緊急連絡や個別連絡を即時配信。
- **YouTube簡易動画配信（ムービー）**: 運動会や発表会などの動画をYouTube限定公開URLを用いて保護者へ安全に共有。全学年共通またはクラス別の視聴制限、公開期限の自動制御、Cookieレス（`youtube-nocookie.com`）によるプライバシー保護再生に対応。

### 4.3 児童記録・健康管理・指導要録

- **児童観察記録**: 日常の様子やエピソードを記録。独自項目UUIDにより、園独自の評価軸を柔軟に追加可能。
- **設定版楽観ロック**: 複数担任による同時編集時の上書き事故を完全に防止。
- **健康記録**: アレルギー情報、体温推移、定期身体測定データのグラフ表示と成長曲線の可視化。

### 4.4 請求管理 & 全銀Zengin口座振替

- **延長料金自動計算**: 登降園時刻の実績データから、標準時間・短時間枠、スポット・月極契約に応じた延長保育料を自動集計。
- **全銀フォーマット出力**: 全銀協規定のフォーマットに準拠した振替依頼データ（固定長テキスト）を生成し、ネットバンキングで一括引落可能。

### 4.5 セキュリティ & 内部統制

- **メールOTP (MFA)**: ログイン時に6桁の一時パスコードを登録メールアドレスに送信。専用認証アプリ不要で高いセキュリティを確保。
- **機微情報監査ログ**: 園児・保護者の個人情報や口座情報の閲覧・変更履歴を漏れなく追跡記録。
- **レートリミット**: ブルートフォース攻撃やDoS攻撃を防ぎ、メモリリークを防止する自動クリーンアップ機構を内蔵。

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

---

## 6. ドキュメント体系

用途に合わせて以下の充実したドキュメントをご参照ください。

- 📖 **[MANUAL.md (総合操作・運用マニュアル)](MANUAL.md)**: 園長・保育士・事務・管理者向けの実践的利用手順書
- ❓ **[Q_AND_A.md (よくある質問と回答)](Q_AND_A.md)**: 現場の疑問、ソルバーの仕組み、トラブルシューティング集
- 🎨 **[ランディングページ (landing_page/index.html)](landing_page/index.html)**: nusoft の魅力を伝える美麗なプロダクト紹介LP
- 🛠️ **[docs/technical-guide.md](docs/technical-guide.md)**: アーキテクチャ設計・API仕様・開発者向けガイド
- 📋 **[docs/features.md](docs/features.md)**: 実装済み機能の網羅的一覧

---

## 7. ライセンス・免責事項

### ライセンス
本プロジェクトは [MIT License](LICENSE) のもとで公開されているオープンソースソフトウェアです。商用利用、改変、再配布が自由に行えます。

### 免責事項
本ソフトウェアが提供する職員配置基準の計算結果およびシフト自動作成機能は、数理最適化モデルに基づく提案です。最終的な法令遵守・人員配置の確認および安全管理責任は、各施設の施設長・管理者が負うものとします。