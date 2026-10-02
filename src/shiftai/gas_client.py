"""Google Apps Script Web App 連携クライアント。

HTTP クライアントは標準ライブラリ ``urllib.request`` のみを使う。
``import`` しただけでは通信しない（Streamlit の起動を妨げない）。
すべての通信エラーは :class:`GasError` に統一して包む。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

ENV_URL = "SHIFTAI_GAS_URL"
ENV_SHEET = "SHIFTAI_GAS_SHEET"
ENV_SECRET = "SHIFTAI_GAS_SECRET"
ENV_TIMEOUT = "SHIFTAI_GAS_TIMEOUT"

DEFAULT_TIMEOUT = 10

SHEET_ALIASES: dict[str, str] = {
    "children": "attendance",
    "attendance": "attendance",
    "staff": "staff",
    "preferences": "希望休",
    "shift": "shift",
}
"""論理名を Apps Script 側のシート名へ写す。"""


class GasError(RuntimeError):
    """GAS 連携で発生したすべてのエラー。"""


class GasConfigError(RuntimeError):
    """GAS 連携の設定が不正なときに送出する。"""


@dataclass
class GasConfig:
    """Apps Script Web App の接続設定。"""

    base_url: str
    sheet: str = "attendance"
    timeout_sec: int = DEFAULT_TIMEOUT
    secret: str = ""

    @classmethod
    def from_env(cls) -> GasConfig | None:
        """環境変数から設定を読む。``SHIFTAI_GAS_URL`` が無ければ ``None``。"""
        base_url = (os.environ.get(ENV_URL) or "").strip()
        if not base_url:
            return None
        raw_timeout = (os.environ.get(ENV_TIMEOUT) or "").strip()
        timeout = DEFAULT_TIMEOUT
        if raw_timeout:
            try:
                timeout = int(float(raw_timeout))
            except ValueError:
                timeout = DEFAULT_TIMEOUT
        return cls(
            base_url=base_url,
            sheet=(os.environ.get(ENV_SHEET) or "attendance").strip() or "attendance",
            timeout_sec=timeout,
            secret=(os.environ.get(ENV_SECRET) or "").strip(),
        )

    def endpoint(self, action: str, *, with_secret: bool = False, **params: str) -> str:
        """action とクエリを付けた URL を返す。

        ``secret`` は既定では **クエリに載せない**。GET のクエリは
        Web アプリ側のアクセスログ・ブラウザの履歴・Referer ヘッダに
        平文で残るため、機密値は POST のボディかヘッダで渡す。
        ``with_secret=True`` を明示したときだけクエリに含める
        （旧来の呼び出しとの互換用。通常は使わない）。
        """
        query: dict[str, str] = {"action": action}
        if self.sheet:
            query["sheet"] = self.sheet
        if with_secret and self.secret:
            query["secret"] = self.secret
        for key, value in params.items():
            if value is not None:
                query[key] = str(value)
        separator = "&" if "?" in self.base_url else "?"
        return f"{self.base_url}{separator}{urllib.parse.urlencode(query)}"


def is_configured(cfg: GasConfig | None = None) -> bool:
    """設定が有効かどうか（URL が埋まっているか）。"""
    config = cfg if cfg is not None else GasConfig.from_env()
    return bool(config and config.base_url and config.base_url.strip())


def available() -> bool:
    """UI の出し分け用。GAS 連携を利用できるなら True。"""
    return is_configured()


def _request_json(
    url: str,
    payload: Mapping[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
) -> dict:
    """GET / POST して JSON オブジェクトを返す。例外はすべて GasError。"""
    if not url:
        raise GasConfigError("GAS の URL が設定されていません")
    data: bytes | None = None
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "shiftai-gas-client/1.0",
        **(dict(headers) if headers else {}),
    }
    method = "GET"
    if payload is not None:
        method = "POST"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except OSError:
            detail = ""
        raise GasError(f"GAS が HTTP {exc.code} を返しました: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise GasError(f"GAS に接続できません: {exc.reason}") from exc
    except TimeoutError as exc:
        raise GasError(f"GAS が {timeout} 秒以内に応答しませんでした") from exc
    except OSError as exc:
        raise GasError(f"GAS への通信に失敗しました: {exc}") from exc

    text = body.decode("utf-8", "replace").strip()
    if not text:
        raise GasError("GAS の応答が空でした")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GasError(f"GAS の応答が JSON ではありません: {text[:300]}") from exc
    if not isinstance(parsed, dict):
        raise GasError(f"GAS の応答形式が不正です: {type(parsed).__name__}")
    if parsed.get("ok") is False:
        raise GasError(
            f"GAS がエラーを返しました: {parsed.get('error') or parsed.get('message') or parsed}"
        )
    return parsed


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value != value:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            return str(value)
    return value


def frame_to_payload(df: pd.DataFrame) -> dict[str, list[Any]]:
    """DataFrame を ``{columns, data}`` に変換する。"""
    columns = [str(c) for c in df.columns]
    rows: list[list[Any]] = []
    for _, series in df.iterrows():
        rows.append([_cell(v) for v in series.tolist()])
    return {"columns": columns, "data": rows}


def payload_to_frame(payload: Mapping[str, Any]) -> pd.DataFrame:
    """``{columns, data}`` または ``{data}`` を DataFrame にする。"""
    data = payload.get("data")
    columns = payload.get("columns")
    if not isinstance(data, list):
        return pd.DataFrame(columns=list(columns) if isinstance(columns, list) else [])
    if not data:
        return pd.DataFrame(columns=list(columns) if isinstance(columns, list) else [])
    if not isinstance(columns, list) or not columns:
        columns = [f"col{i}" for i in range(len(data[0]))]
    return pd.DataFrame(data, columns=columns)


class GoogleAppsScriptClient:
    """Apps Script Web App（doGet / doPost の JSON API）と話す。"""

    def __init__(self, config: GasConfig):
        if config is None or not getattr(config, "base_url", "").strip():
            raise GasConfigError("GasConfig.base_url が空です")
        self.config = config

    def __repr__(self) -> str:
        return f"GoogleAppsScriptClient(url={self.config.base_url!r}, sheet={self.config.sheet!r})"

    def _get(self, action: str, **params: str) -> dict:
        """読み取り系（ping / read）を実行する。

        secret が設定されている場合は **POST のボディ**で送る。GET の
        クエリ文字列は Web アプリ側のアクセスログやブラウザ履歴に
        平文で残り、秘密値が漏れるため。``doPost`` も同じ
        ``handle()`` を経由するので既存のデプロイ定義のままで動く。
        """
        if not self.config.secret:
            return _request_json(
                self.config.endpoint(action, **params), None, self.config.timeout_sec
            )
        body: dict[str, Any] = {"action": action, "sheet": self.config.sheet, **params}
        body["secret"] = self.config.secret
        return _request_json(self.config.base_url, body, self.config.timeout_sec)

    def _post(self, payload: Mapping[str, Any]) -> dict:
        body = dict(payload)
        body.setdefault("secret", self.config.secret)
        return _request_json(self.config.base_url, body, self.config.timeout_sec)

    def ping(self) -> dict:
        """疎通確認。``{"ok": True, "spreadsheet": ...}`` が返る。"""
        return self._get("ping")

    def fetch_table(self, sheet: str | None = None) -> pd.DataFrame:
        """シートを DataFrame として取得する。"""
        payload = self._get("read", sheet=sheet or self.config.sheet)
        return payload_to_frame(payload)

    def push_table(self, df: pd.DataFrame, sheet: str, *, mode: str = "replace") -> dict:
        """DataFrame をシートへ送る。``mode`` は ``replace`` / ``append``。"""
        if mode not in ("replace", "write", "append"):
            raise GasConfigError(f"mode は 'replace' か 'append' にしてください（got {mode!r}）")
        action = "append" if mode == "append" else "write"
        body = {"action": action, "sheet": sheet}
        body.update(frame_to_payload(df))
        return self._post(body)

    def push_children(self, df: pd.DataFrame) -> dict:
        return self.push_table(df, SHEET_ALIASES["children"], mode="replace")

    def push_staff(self, df: pd.DataFrame) -> dict:
        return self.push_table(df, SHEET_ALIASES["staff"], mode="replace")

    def push_shift(self, df: pd.DataFrame) -> dict:
        return self.push_table(df, SHEET_ALIASES["shift"], mode="replace")

    def sync_all(self, tables: Mapping[str, pd.DataFrame]) -> dict[str, dict]:
        """複数テーブルをまとめて送信する（1 つ失敗したら GasError）。"""
        results: dict[str, dict] = {}
        for name, frame in tables.items():
            sheet = SHEET_ALIASES.get(name, name)
            results[name] = self.push_table(frame, sheet, mode="replace")
        return results

    @staticmethod
    def to_dataframe_payload(df: pd.DataFrame) -> str:
        """DataFrame を JSON 文字列にする（デバッグ・保存用）。"""
        return json.dumps(frame_to_payload(df), ensure_ascii=False)
