# Google Apps Script 連携（登降園予定データ集約）

`shiftai` の Streamlit アプリから、Google  Sheets の「園児登降園予定」「職員」「シフト」を
直接読み書きするための Web App 設定手順です。**コピペで再現できる粒度**で記述しています。

---

## 0. 全体像

```
Streamlit (shiftai)  ──HTTP(JSON)──▶  Apps Script Web App  ──SpreadsheetApp──▶  Google シート
   gas_client.py                      gas/Code.gs            SHEETS マップ
```

- 通信は **GET**（`ping` / `read`）と **POST**（`write` / `append`）だけです。
- クライアントは標準ライブラリ `urllib.request` のみを使用します（`requests` は不要）。
- HTTP クライアントは **GAS の `/exec` URL** を使います（`/dev` は使いません）。

---

## 1. Google シートを作る

1. <https://sheets.new> を開く
2.  Untitled spreadsheet の名前を **`保育.shift`** など付ける
3. タブ名を次の 3 つに変更する（`Code.gs` の `SHEETS` マップと一致させる）

   | 論理名      | タブ名      | 用途                     |
   | ----------- | ----------- | ------------------------ |
   | `attendance` | `登降園予定` | 園児の登降園予定CSV      |
   | `staff`      | `職員`      | 職員CSV                  |
   | `shift`      | `シフト`    | 出力したシフトCSV        |

4. シートを閉じる
5. ブラウザの URL から **ID** を取り出す

   ```
   https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit#gid=0
                                                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
   ```

   この `1AbC...` が `SPREADSHEET_ID` です。

> `SHEETS` マップに無い名前（例: `希望休`）を要求した場合、`Code.gs` は
> **その名前のタブが存在すればそれを使い、無ければ `sheet not found` エラー**にします。
> `希望休` タブを追加してあれば `preferences` も同じ経路で書き込めます。

---

## 2. Apps Script プロジェクトを作る

1. 作成したシートを開き、URL の末尾にある **「拡張機能」→「Apps スクリプト」** をクリック
2. 「新しいプロジェクトを作成」を押す
3. 左ペインの **「コード」** を選び、`Code.gs` の内容をすべて削除する
4. このリポジトリの **`gas/Code.gs` の中身をそのままコピペ**する
5. 「保存」（Ctrl+S / Cmd+S）

---

## 3. スクリプトプロパティを設定する

1. Apps Script エディタ左上 gear アイコン → **「プロジェクトの設定」**
2. **「スクリプト プロパティ」** セクションで **「スクリプト プロパティを追加」** を 2 回クリック
3. 次の 2 行を入力する

   | プロパティ名       | 値                                   |
   | ------------------ | ------------------------------------ |
   | `SPREADSHEET_ID`   | 手順 1 でコピーした ID              |
   | `SECRET`           | 自由に決めた長いランダム文字列      |

4. 「保存」

> `SECRET` は **クライアントと GAS で同じ値** にする必要があります。
> 値で `abcdef0123456789...` のような 32 文字以上のランダム文字列を使うと安全です
> （`openssl rand -hex 32` で生成できます）。

---

## 4. デプロイする（`/exec`）

1. エディタ右上の **「デプロイ」→「新しいデプロイ」** をクリック
2. Deployment type に **「ウェブアプリ」** を選ぶ
3. **「次のユーザーとして実行」**: `自分`（自分だけが使う場合）
4. **「アクセスできるユーザー」**: `自分`（より広く共有する場合は `任意のユーザー`）
   - 組織で外部に公開する場合は `任意のユーザー` を選びますが、`SECRET` 認証が唯一の防御です
5. **「デプロイ」** → Google アカウントの認証を済ませる
6. 完了後に表示される **「ウェブ アプリの URL」** をコピーする

   ```
   https://script.google.com/macros/s/AKfycbXXXXXXXX/exec
   ```

7. **この URL を Streamlit 側に設定します**（手順 7）

> `manage deployments` に **Authorized domain（許可されたドメイン）** に関する
> エラーが出た場合は、シートが「リンクDiamond 共有」か「My ドメイン」に置かれているか確認してください。

---

## 5. 動作確認（curl）

`BASE` に手順 4 の URL、`SECRET` に手順 3 の値を代入します。

```bash
BASE='https://script.google.com/macros/s/AKfycbXXXXXXXX/exec'
SECRET='手順3で設定した値'
```

### 5-1. 疎通確認

```bash
curl -sS -G "$BASE" \
  --data-urlencode "action=ping" \
  --data-urlencode "secret=$SECRET"
```

期待レスポンス:

```json
{"ok":true,"action":"ping","message":"connection ok","version":"1.0.0",
 "rows":0,"columns":[],"data":[],"updatedAt":"2026-09-28T15:04:05+09:00"}
```

### 5-2. シートを読む

```bash
curl -sS -G "$BASE" \
  --data-urlencode "action=read" \
  --data-urlencode "sheet=attendance" \
  --data-urlencode "secret=$SECRET" | python3 -m json.tool
```

### 5-3. シートへ書き込む（CSV の中身をそのまま送る）

```bash
curl -sS -X POST "$BASE" \
  -H 'Content-Type: application/json; charset=utf-8' \
  -d '{
        "action": "write",
        "sheet": "attendance",
        "secret": "'"$SECRET"'",
        "columns": ["園児ID","氏名","年齢","登園日","登園時刻","降園時刻"],
        "data": [
          ["C001","佐藤 さくら",2,"2026-09-28","08:30","17:00"],
          ["C002","鈴木 ひなた",1,"2026-09-28","08:45","17:15"]
        ]
      }' | python3 -m json.tool
```

### 5-4. 末尾追記（`append`）

```json
{ "action": "append", "sheet": "attendance", "secret": "...",
  "columns": ["園児ID","氏名"], "data": [["C003","高橋 そら"]] }
```

### 5-5. Python クライアントから確認

```python
from shiftai.gas_client import GasConfig, GoogleAppsScriptClient

cfg = GasConfig(
    base_url="https://script.google.com/macros/s/AKfycbXXXXXXXX/exec",
    sheet="attendance",
    secret="手順3の値",
    timeout_sec=10,
)
client = GoogleAppsScriptClient(cfg)
print(client.ping())
print(client.fetch_table("attendance"))
```

---

## 6. コードを編集したら `/dev` ではなく **再デプロイ** が必要です

Apps Script の **編集画面の URL は `/dev`** ですが、**`/dev` の URL は非公開なので
クライアントからは叩けません**。コードを変更したら必ず:

1. エディタで **「デプロイ」→「デプロイの更新」→「新しいバージョン」** を選ぶ
2. バージョンメモ（例: `1.1.0 - write 対応`）を記入して **「デプロイ」**
3. 変更後の挙動を `curl`（手順 5）で確認する

> `/dev` で実行して「動作を確認」ボタンを押すと、そのバージョンのログを
> エディタ下部の実行結果で確認できます。Web アプリ経由のログは
> **「デプロイ」→「デプロイの履歴」→ 該当バージョン「 Logs を表示」** に出ます。

---

## 7. Streamlit 側の環境変数設定

```bash
export SHIFTAI_GAS_URL='https://script.google.com/macros/s/AKfycbXXXXXXXX/exec'
export SHIFTAI_GAS_SHEET='attendance'
export SHIFTAI_GAS_SECRET='手順3で設定した値'
export SHIFTAI_GAS_TIMEOUT='10'
streamlit run streamlit_app.py
```

`.streamlit/secrets.toml` を使う場合（`secrets.toml` は `.gitignore` 必ずに入れてください）:

```toml
[gas]
base_url = "https://script.google.com/macros/s/AKfycbXXXXXXXX/exec"
sheet = "attendance"
secret = "手順3で設定した値"
timeout_sec = 10
```

`GasConfig.from_env()` は **`SHIFTAI_GAS_URL` が無いと `None` を返します**（例外を投げません）。
UI では `shiftai.gas_client.available()` / `is_configured()` を見て
GAS 連携セクションの表示を出し分けます。

```python
from shiftai.gas_client import available, is_configured, GasConfig, GoogleAppsScriptClient

if is_configured():  # UI の出し分け（通信はしない）
    cfg = GasConfig.from_env()
    client = GoogleAppsScriptClient(cfg)  # ここで初めて通信する
    st.button("GAS へ同期", on_click=lambda: client.sync_all(tables))
```

---

## 8. `gas/Code.gs` の関数一覧

| 関数 | 概要 | 呼び出し元 |
| --- | --- | --- |
| `doGet(e)` | Web アプリ GET 入口。`handle(e)` へ委譲 | GAS |
| `doPost(e)` | Web アプリ POST 入口。`handle(e)` へ委譲 | GAS |
| `handle(e)` | action 分岐・認証・例外ハンドリングの一括処理 | `doGet` / `doPost` |
| `readBody(e)` | POST 本文を JSON として読む（失敗時は `{}`） | `handle` |
| `verifySecret(given)` | スクリプトプロパティ `SECRET` と照合。不一致で送出 | `handle` |
| `resolveSheet(name)` | 論理名 → 実シート名。未登録かつタブも無い場合は送出 | `handle` |
| `openSpreadsheet()` | `SPREADSHEET_ID` で `SpreadsheetApp.openById` | `resolveSheet` / `readSheet` / `writeSheet` |
| `readSheet(sheetName)` | `getDisplayValues` で 2 次元配列を取得し、空行を除外 | `handle`（`read`） |
| `writeSheet(sheetName, body, action)` | `write` = 全消去して書き直し、`append` = 末尾追記 | `handle`（`write` / `append`） |
| `toStringMatrix(value)` | 2 次元配列を全要素文字列へ変換 | `writeSheet` |
| `guessHeader(data)` | ヘッダ欠落時の代用列名 `列1, 列2, ...` | `writeSheet` |
| `respond(payload)` | `ContentService.createTextOutput(...).setMimeType(JSON)` | 全処理 |
| `now()` | Asia/Tokyo の ISO8601 タイムスタンプ | `respond` |
| `selfTest()` | エディタで手実行できる動作確認用 | 手動 |

定数:

| 名前 | 値 / 意味 |
| --- | --- |
| `VERSION` | `'1.0.0'` — `ping` の応答に含まれる |
| `SHEETS` | `{attendance: '登降園予定', staff: '職員', shift: 'シフト'}` |
| `ACTIONS` | `['ping', 'read', 'write', 'append']` — これ以外は `unknown action` |

---

## 9. 典型的なエラーと対処

| エラーメッセージ | 原因 | 対処 |
| --- | --- | --- |
| `SECRET Mismatch` | スクリプトプロパティの `SECRET` とクライアントの `SHIFTAI_GAS_SECRET` が不一致 | どちらかを合わせて再デプロイ（プロパティ変更はデプロイ不要だが、クライアント側は再起動） |
| `SECRET is not set` | スクリプトプロパティ `SECRET` が未設定 | 手順 3 で追加 |
| `SPREADSHEET_ID is not set` | スクリプトプロパティ `SPREADSHEET_ID` が未設定 | 手順 3 で追加 |
| `sheet not found: <name>` | タブ名が違う、またはタブが作られていない | シートのタブ名を手順 1 の表に合わせる。`SHEETS` マップを編集して再デプロイしてもよい |
| `unknown action: <name>` | 想定外の action | クライアントは `ping/read/write/append` のみ送信する |
| `HTTP 404` | `/dev` の URL を使っている、または 存在しないデプロイ | **必ず `/exec` の URL** を使う。コード変更後は「デプロイの更新」 |
| `HTTP 401 / 403` | 「実行するユーザー」のアカウントで認証していない | 再度デプロイして認証するか、アクセスできるユーザーを `任意のユーザー` にする |
| `CORS` / ブラウザのコンソールエラー | Apps Script はブラウザからの CORS を許さない | 問題なし。`shiftai.gas_client` は **サーバー側（Streamlit プロセス）** から HTTP を投げるため CORS が発生しない |
| `GAS が n 秒以内に応答しませんでした` | タイムアウト | `SHIFTAI_GAS_TIMEOUT` を 20 程度に上げる（初回アクセスは-cold start- で遅い） |
| `GAS の応答が JSON ではありません` | `/exec` ではなくGoogle ログイン画面が返っている | 認証済みアカウントで URL を開くか、`/exec` の URL が正しいか確認 |
| `GAS に接続できません: <reason>` | ネットワーク遮断・プロキシ | 社外通信が許可されているか確認。UI は `GasError` を握ってエラー表示のみ行う |

---

## 10. セキュリティ上の注意

- `SECRET` は **スクリプトプロパティ**（ソース上には書かない）に置いてください。
- シートそのものは「リンク共有"Who has access"を **限定ユーザー** にしてください。
  シートの公開と Web アプリの公開は別の話で、**`SECRET` 認証はシートの共有を代替しません**。
- `SECRET` を漏らした場合、`SECRET` を即座に再生成 → 再デプロイ してください。
- `doGet` にも `verifySecret` が入っているため、URL に `secret=` を付けただけの GET でも保護されます。
- **`SECRET` は GET のクエリ文字列に載せないでください**（2026-09-29 のクライアント変更）。
  クエリは Web アプリ側のアクセスログ・ブラウザの履歴・`Referer` ヘッダに
  平文で残り、漏えいに直結します。`gas_client` は `SHIFTAI_GAS_SECRET` が
  設定されている場合、読み取り系（`ping` / `read`）を **POST のボディ**で
  送信します（`doPost` も同じ `handle()` を経由するのでデプロイ定義のままで動きます）。
  旧来の GET クエリ方式が必要なら `GasConfig.endpoint(action, with_secret=True)`
  を明示的に指定してください。
- `verifySecret` は長さによらず一定の時間で比較する `timingSafeEqual` を使います。
  単純な `!==` 比較は応答時間の差から `SECRET` を 1 文字ずつ推測できるためです。
