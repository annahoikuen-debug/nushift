"""GAS 連携クライアント（``shiftai.gas_client``）のテスト。

**このテストはネットワークを一切出さない。**
接続試験には必ず ``http://127.0.0.1:9/``（discard ポート、接続即拒否）を使う。
``import shiftai.gas_client`` だけでは通信しないことも保証する。
"""

from __future__ import annotations

import json
import urllib.error
from datetime import date

import pandas as pd
import pytest

from shiftai import gas_client
from shiftai.gas_client import (
    DEFAULT_TIMEOUT,
    ENV_SECRET,
    ENV_SHEET,
    ENV_TIMEOUT,
    ENV_URL,
    SHEET_ALIASES,
    GasConfig,
    GasConfigError,
    GasError,
    GoogleAppsScriptClient,
    _request_json,
    available,
    frame_to_payload,
    is_configured,
    payload_to_frame,
)

pytestmark = pytest.mark.timeout(300)

DEAD_URL = "http://127.0.0.1:9/exec"
"""TCP ポート 9（discard）は listening していないので接続は即座に拒否される。"""


@pytest.fixture(autouse=True)
def _clear_gas_env(monkeypatch):
    """テスト環境に GAS 環境変数が漏れないようにする。"""
    for name in (ENV_URL, ENV_SHEET, ENV_SECRET, ENV_TIMEOUT):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config() -> GasConfig:
    """接続先 localhost 設定（実際に叩かない）。"""
    return GasConfig(base_url=DEAD_URL, sheet="attendance", timeout_sec=2, secret="s3cret")


# ---------------------------------------------------------------------------
# 設定の読み込み
# ---------------------------------------------------------------------------


def test_from_env_未設定ならNone():
    """``SHIFTAI_GAS_URL`` が無ければ ``None`` を返す（例外ではない）。"""
    assert GasConfig.from_env() is None
    assert not is_configured()
    assert not available()


def test_from_env_全項目を読む(monkeypatch):
    """4 つの環境変数を正しく読むこと。"""
    monkeypatch.setenv(ENV_URL, DEAD_URL)
    monkeypatch.setenv(ENV_SHEET, "staff")
    monkeypatch.setenv(ENV_SECRET, "abc123")
    monkeypatch.setenv(ENV_TIMEOUT, "25")
    cfg = GasConfig.from_env()
    assert cfg is not None
    assert cfg.base_url == DEAD_URL
    assert cfg.sheet == "staff"
    assert cfg.secret == "abc123"
    assert cfg.timeout_sec == 25
    assert is_configured(cfg)
    assert available()


def test_from_env_シート既定値(monkeypatch):
    """``SHIFTAI_GAS_SHEET`` 未設定なら ``attendance`` が既定になること。"""
    monkeypatch.setenv(ENV_URL, DEAD_URL)
    cfg = GasConfig.from_env()
    assert cfg.sheet == "attendance"
    assert cfg.timeout_sec == DEFAULT_TIMEOUT
    assert cfg.secret == ""


def test_from_env_タイムアウト不正は既定値(monkeypatch):
    """``SHIFTAI_GAS_TIMEOUT`` が壊れていても既定値へフォールバックすること。"""
    monkeypatch.setenv(ENV_URL, DEAD_URL)
    monkeypatch.setenv(ENV_TIMEOUT, "abc")
    assert GasConfig.from_env().timeout_sec == DEFAULT_TIMEOUT


def test_from_env_空白は未設定扱い(monkeypatch):
    """空白だけの ``SHIFTAI_GAS_URL`` は未設定として扱われること。"""
    monkeypatch.setenv(ENV_URL, "   ")
    assert GasConfig.from_env() is None


def test_endpointにactionと追加パラメータが付く(config):
    """URL が action/sheet/追加パラメータを含むこと。"""
    url = config.endpoint("read", extra="1")
    assert url.startswith(DEAD_URL)
    assert "action=read" in url
    assert "sheet=attendance" in url
    assert "extra=1" in url


def test_endpointは既定でsecretをクエリに載せない(config):
    """GET のクエリに secret を載せないこと（アクセスログへの漏えい防止）。"""
    assert "secret" not in config.endpoint("read")
    assert "s3cret" not in config.endpoint("read")


def test_endpointは明示指定時だけsecretをクエリに載せる(config):
    """後方互換用に ``with_secret=True`` でクエリへ載せられること。"""
    url = config.endpoint("read", with_secret=True)
    assert "secret=s3cret" in url


def test_secret付き読み取りはPOSTのボディで送る(config, monkeypatch):
    """secret 設定時は GET ではなく POST のボディで送ることを確認する。"""
    seen: dict = {}

    def fake(url, payload=None, timeout=10, headers=None):
        seen["url"] = url
        seen["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(gas_client, "_request_json", fake)
    gas_client.GoogleAppsScriptClient(config).fetch_table()
    assert seen["payload"]["secret"] == "s3cret"
    assert seen["payload"]["action"] == "read"
    assert "secret=" not in seen["url"]


def test_secret無しはGETのまま(config, monkeypatch):
    """secret 未設定なら従来どおり GET を使うこと。"""
    seen: dict = {}

    def fake(url, payload=None, timeout=10, headers=None):
        seen["url"] = url
        seen["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(gas_client, "_request_json", fake)
    plain = GasConfig(base_url=DEAD_URL, sheet="attendance", timeout_sec=2)
    gas_client.GoogleAppsScriptClient(plain).ping()
    assert seen["payload"] is None
    assert "action=ping" in seen["url"]


def test_endpoint_既存クエリを壊さない():
    """元 URL に ``?`` があれば ``&`` で連結すること。"""
    cfg = GasConfig(base_url="https://example.invalid/x?a=b", sheet="")
    assert "a=b&action=ping" in cfg.endpoint("ping")


# ---------------------------------------------------------------------------
# クライアントの生成
# ---------------------------------------------------------------------------


def test_空URLでGasConfigError():
    """``base_url`` が空のクライアントは作れないこと。"""
    with pytest.raises(GasConfigError):
        GoogleAppsScriptClient(GasConfig(base_url=""))
    with pytest.raises(GasConfigError):
        GoogleAppsScriptClient(None)


def test_reprにURLが出る(config):
    """デバッグ用の ``repr`` が URL とシート名を含むこと。"""
    text = repr(GoogleAppsScriptClient(config))
    assert DEAD_URL in text
    assert "attendance" in text


def test_push_tableの不正モード(config):
    """``mode`` が replace/append 以外は ``GasConfigError`` にすること。"""
    client = GoogleAppsScriptClient(config)
    with pytest.raises(GasConfigError):
        client.push_table(pd.DataFrame({"a": [1]}), "shift", mode="upsert")


# ---------------------------------------------------------------------------
# ネットワーク遮断（すべて 127.0.0.1:9 への接続拒否で確かめる）
# ---------------------------------------------------------------------------


def test_importだけでは通信しない():
    """``import`` はネットワーク IO をしない（Streamlit 起動を妨げない）。"""
    import importlib

    module = importlib.import_module("shiftai.gas_client")
    assert module.DEFAULT_TIMEOUT == DEFAULT_TIMEOUT
    assert module.GasConfig.from_env() is None


def test_pingはGasError(config):
    """接続拒否が ``GasError`` に統一されていること。"""
    client = GoogleAppsScriptClient(config)
    with pytest.raises(GasError):
        client.ping()


def test_fetch_tableはGasError(config):
    """シート取得の失敗が ``GasError`` になること。"""
    client = GoogleAppsScriptClient(config)
    with pytest.raises(GasError):
        client.fetch_table()


def test_push_childrenはGasError(config):
    """園児データ送信の失敗が ``GasError`` になること。"""
    client = GoogleAppsScriptClient(config)
    with pytest.raises(GasError):
        client.push_children(pd.DataFrame({"園児ID": ["C001"]}))


def test_push_staffはGasError(config):
    """職員データ送信の失敗が ``GasError`` になること。"""
    client = GoogleAppsScriptClient(config)
    with pytest.raises(GasError):
        client.push_staff(pd.DataFrame({"職員ID": ["S001"]}))


def test_push_shiftはGasError(config):
    """シフト送信の失敗が ``GasError`` になること。"""
    client = GoogleAppsScriptClient(config)
    with pytest.raises(GasError):
        client.push_shift(pd.DataFrame({"職員ID": ["S001"]}))


def test_sync_allはGasError(config):
    """一括送信の失敗が ``GasError`` になること。"""
    client = GoogleAppsScriptClient(config)
    with pytest.raises(GasError):
        client.sync_all({"children": pd.DataFrame({"園児ID": ["C001"]})})


def test_sync_allは部分失敗を握り潰さない(config, monkeypatch):
    """1 枚でも失敗すれば ``sync_all`` は例外を送出する（結果を握り潰さない）。"""
    client = GoogleAppsScriptClient(config)
    calls: list[str] = []

    def fake_post(payload):
        calls.append(payload.get("sheet", ""))
        if len(calls) == 2:
            raise GasError("2 枚目で失敗")
        return {"ok": True}

    monkeypatch.setattr(client, "_post", fake_post)
    with pytest.raises(GasError) as excinfo:
        client.sync_all(
            {
                "children": pd.DataFrame({"園児ID": ["C001"]}),
                "staff": pd.DataFrame({"職員ID": ["S001"]}),
                "shift": pd.DataFrame({"職員ID": ["S001"]}),
            }
        )
    assert "2 枚目で失敗" in str(excinfo.value)
    assert calls == [SHEET_ALIASES["children"], SHEET_ALIASES["staff"]]


def test_sync_allは成功時に全結果を返す(config, monkeypatch):
    """全テーブルが成功したら ``{名前: 応答}`` を返すこと。"""
    client = GoogleAppsScriptClient(config)
    monkeypatch.setattr(client, "_post", lambda payload: {"ok": True, "sheet": payload["sheet"]})
    out = client.sync_all({"children": pd.DataFrame({"園児ID": ["C001"]}),
                           "staff": pd.DataFrame({"職員ID": ["S001"]})})
    assert set(out) == {"children", "staff"}
    assert all(v["ok"] for v in out.values())


def test_空URLの送信はGasConfigError():
    """URL が空なら ``GasConfigError`` を送出し、外への通信に行かないこと。"""
    with pytest.raises(GasConfigError):
        _request_json("")


# ---------------------------------------------------------------------------
# HTTP 応答のパース（実際の通信は不要）
# ---------------------------------------------------------------------------


class _FakeResponse:
    """``urllib.request.urlopen`` のコンテキストマネージャに成り立つ偽レスポンス。"""

    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_HTTPエラーをGasErrorに(monkeypatch):
    """``HTTPError`` が ``GasError`` に包まれること。"""

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(DEAD_URL, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(GasError) as excinfo:
        _request_json(DEAD_URL, timeout=1)
    assert "404" in str(excinfo.value)


def test_URLErrorをGasErrorに(monkeypatch):
    """``URLError`` が ``GasError`` に包まれること。"""

    def boom(request, timeout=None):
        raise urllib.error.URLError(OSError("Connection refused"))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(GasError) as excinfo:
        _request_json(DEAD_URL, timeout=1)
    assert "接続できません" in str(excinfo.value)


def test_タイムアウトをGasErrorに(monkeypatch):
    """``socket.timeout`` が ``GasError`` に包まれること。"""

    def boom(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(GasError) as excinfo:
        _request_json(DEAD_URL, timeout=3)
    assert "3 秒以内" in str(excinfo.value)


def test_空レスポンスはGasError(monkeypatch):
    """空の応答は ``GasError`` にすること。"""
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _FakeResponse(b"   ")
    )
    with pytest.raises(GasError):
        _request_json(DEAD_URL, timeout=1)


def test_JSONでない応答はGasError(monkeypatch):
    """JSON でない応答は ``GasError`` にすること。"""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse(b"<html>login</html>"),
    )
    with pytest.raises(GasError) as excinfo:
        _request_json(DEAD_URL, timeout=1)
    assert "JSON" in str(excinfo.value)


def test_配列応答はGasError(monkeypatch):
    """JSON オブジェクトでない応答は ``GasError`` にすること。"""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse(b"[1, 2, 3]"),
    )
    with pytest.raises(GasError):
        _request_json(DEAD_URL, timeout=1)


def test_ok_falseの応答はGasError(monkeypatch):
    """``{"ok": false, "error": ...}`` が ``GasError`` にすること。"""
    body = json.dumps({"ok": False, "error": "SECRET mismatch"}).encode("utf-8")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _FakeResponse(body)
    )
    with pytest.raises(GasError) as excinfo:
        _request_json(DEAD_URL, timeout=1)
    assert "SECRET mismatch" in str(excinfo.value)


def test_正常応答をdictで返す(monkeypatch):
    """正常な ``{"ok": true, ...}`` は辞書で返ること。"""
    body = json.dumps({"ok": True, "action": "ping", "version": "1.0.0"}).encode("utf-8")
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _FakeResponse(body)
    )
    assert _request_json(DEAD_URL, timeout=1)["action"] == "ping"


def test_POSTはJSONを送る(monkeypatch):
    """``payload`` を渡すと POST で JSON 本文を送ること。"""
    seen: dict = {}

    def fake_urlopen(request, timeout=None):
        seen["method"] = request.get_method()
        seen["body"] = request.data
        seen["content_type"] = request.headers.get("Content-type")
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _request_json(DEAD_URL, {"action": "write", "sheet": "attendance"}, timeout=1)
    assert seen["method"] == "POST"
    assert json.loads(seen["body"])["sheet"] == "attendance"
    assert "json" in seen["content_type"]


def test_GETはmethodがGET(monkeypatch):
    """``payload=None`` のときは GET になること。"""
    seen: dict = {}

    def fake_urlopen(request, timeout=None):
        seen["method"] = request.get_method()
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    _request_json(DEAD_URL, None, timeout=1)
    assert seen["method"] == "GET"


# ---------------------------------------------------------------------------
# ペイロード変換
# ---------------------------------------------------------------------------


def test_frame_to_payload():
    """DataFrame が ``{columns, data}`` になること。"""
    frame = pd.DataFrame({"園児ID": ["C001", "C002"], "登園日": [date(2026, 9, 28), date(2026, 9, 29)]})
    payload = frame_to_payload(frame)
    assert payload["columns"] == ["園児ID", "登園日"]
    assert payload["data"][0] == ["C001", "2026-09-28"]


def test_frame_to_payloadの欠損とbool():
    """``NaN`` は空文字、真偽値は ``"true"/"false"`` になること。"""
    frame = pd.DataFrame({"a": [True, False], "b": [None, 1.5]})
    data = frame_to_payload(frame)["data"]
    assert data[0] == ["true", ""]
    assert data[1] == ["false", 1.5]


def test_payload_to_frame():
    """``{columns, data}`` が DataFrame に戻ること。"""
    frame = payload_to_frame({"columns": ["a", "b"], "data": [[1, 2], [3, 4]]})
    assert list(frame.columns) == ["a", "b"]
    assert frame.shape == (2, 2)


def test_payload_to_frame_列なし():
    """``columns`` が無ければ連番の列名を振ること。"""
    frame = payload_to_frame({"data": [[1, 2]]})
    assert list(frame.columns) == ["col0", "col1"]


def test_payload_to_frame_空():
    """``data`` が空なら列だけの空 DataFrame が返ること。"""
    assert payload_to_frame({"columns": ["a"], "data": []}).empty
    assert payload_to_frame({}).empty


def test_round_trip():
    """DataFrame → payload → DataFrame で列名が保たれること。"""
    frame = pd.DataFrame({"職員ID": ["S001"], "氏名": ["山田花子"]})
    assert list(payload_to_frame(frame_to_payload(frame)).columns) == list(frame.columns)


def test_to_dataframe_payloadはJSON文字列():
    """デバッグ用の ``to_dataframe_payload`` が JSON 文字列を返すこと。"""
    text = GoogleAppsScriptClient.to_dataframe_payload(pd.DataFrame({"a": [1]}))
    assert json.loads(text)["columns"] == ["a"]


def test_SHEET_ALIASES():
    """論理名 → Apps Script シート名の対応が UI と一致すること。"""
    assert SHEET_ALIASES["children"] == "attendance"
    assert SHEET_ALIASES["staff"] == "staff"
    assert SHEET_ALIASES["shift"] == "shift"
    assert SHEET_ALIASES["preferences"] == "希望休"
