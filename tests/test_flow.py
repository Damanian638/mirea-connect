"""Сквозной тест: DCR → /authorize → страница входа → code → token → MCP."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from pymirea import _settings
from pymirea.auth import AuthChallenge, AuthResult
from pymirea.schedule import SCHEDULE_SUPABASE_URL
from starlette.testclient import TestClient

from mirea_connect.auth import MireaAuth
from mirea_connect.config import Settings
from mirea_connect.server import make_app

REDIRECT = "http://localhost:9999/callback"
PUBLIC = "http://localhost"


@pytest.fixture
def client(tmp_path):
    prev = _settings._cfg
    settings = Settings(public_url=PUBLIC, data_dir=tmp_path, extra_allowed_hosts=[])
    app = make_app(settings)
    with TestClient(app, base_url=PUBLIC, follow_redirects=False) as c:
        yield c
    _settings._cfg = prev


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(c: TestClient) -> str:
    r = c.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT],
            "client_name": "Test Client",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _start(c: TestClient, client_id: str, challenge: str) -> str:
    r = c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st",
            "scope": "mirea",
            "resource": f"{PUBLIC}/mcp",
        },
    )
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    assert loc.startswith(f"{PUBLIC}/login?req=")
    req = parse_qs(urlparse(loc).query)["req"][0]
    page = c.get(f"/login?req={req}")
    assert page.status_code == 200 and "Test Client" in page.text
    return req


def _token(c: TestClient, client_id: str, redirect: str, verifier: str) -> dict:
    q = parse_qs(urlparse(redirect).query)
    assert q["state"] == ["st"]
    r = c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": q["code"][0],
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


class Mcp:
    def __init__(self, c: TestClient, token: str):
        self.c = c
        self.h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self.id = 0
        r = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        assert "result" in r, r
        self.c.post("/mcp", headers=self.h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _rpc(self, method: str, params: dict) -> dict:
        self.id += 1
        r = self.c.post("/mcp", headers=self.h, json={"jsonrpc": "2.0", "id": self.id, "method": method, "params": params})
        assert r.status_code == 200, (r.status_code, r.text)
        if sid := r.headers.get("mcp-session-id"):
            self.h["mcp-session-id"] = sid
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            datas = [json.loads(m) for m in re.findall(r"^data: (.+)$", r.text, re.M)]
            return [d for d in datas if d.get("id") == self.id][-1]
        return r.json()

    def call(self, name: str, args: dict) -> tuple[bool, str]:
        res = self._rpc("tools/call", {"name": name, "arguments": args})["result"]
        return bool(res.get("isError")), "".join(c.get("text", "") for c in res["content"])


RPC = f"{SCHEDULE_SUPABASE_URL}/rest/v1/rpc"


def _mock_schedule():
    respx.post(f"{RPC}/search_schedule_targets").mock(
        return_value=httpx.Response(200, json=[{"external_id": "5012", "target_title": "ЭФБО-01-24", "full_title": "ЭФБО-01-24"}])
    )
    respx.post(f"{RPC}/get_schedule_for_entity").mock(
        return_value=httpx.Response(
            200,
            json=[{"type": "__lesson_schedule__", "subject": "Физика", "lesson_type": "lecture", "dates": ["10-01-2030"],
                   "lesson_bells": {"start_time": "09:00", "end_time": "10:30"},
                   "classrooms": [{"name": "А-9"}], "groups": ["ЭФБО-01-24"]}],
        )
    )
    respx.post(f"{RPC}/get_free_rooms_window").mock(
        return_value=httpx.Response(200, json=[{"room": "А-9", "campus": "В-78"}, {"room": "10", "campus": "ФФ"}])
    )


def test_mcp_requires_auth(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"].endswith("/register")


@respx.mock
def test_guest_flow_public_tools_only(client):
    _mock_schedule()
    cid = _register(client)
    verifier, challenge = _pkce()
    req = _start(client, cid, challenge)
    r = client.post("/login", data={"req": req, "action": "guest"})
    assert r.status_code == 302
    tok = _token(client, cid, r.headers["location"], verifier)

    m = Mcp(client, tok["access_token"])
    err, text = m.call("timetable", {"kind": "group", "query": "ЭФБО-01-24", "days": 1, "date_from": "2030-01-10"})
    assert not err and "Физика (lecture)" in text
    err, text = m.call("free_rooms", {"start_time": "10:40", "end_time": "12:10", "date": "2030-01-10", "campus": "В-78"})
    assert not err and '"total_free": 1' in text
    err, text = m.call("free_rooms", {"start_time": "10:40", "end_time": "12:10", "rooms": ["А-9 (В-78)", "А-10"]})
    data = json.loads(text)
    assert [r["free"] for r in data["rooms"]] == [True, False]
    err, text = m.call("my_grades", {})
    assert err and "без входа" in text
    err, text = m.call("whoami", {})
    assert '"guest"' in text

    # refresh-ротация: старый refresh больше не работает
    r1 = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid})
    assert r1.status_code == 200
    r2 = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid})
    assert r2.status_code == 400


def test_login_with_otp_and_personal_tool(client, monkeypatch):
    challenge = AuthChallenge(kind="otp", action_url="https://sso.mirea.ru/x", field_name="otp", hidden_fields={})

    async def fake_login(self, username, password):
        assert password == "secret"
        return AuthResult(success=False, message="otp", challenge=challenge)

    async def fake_2fa(self, ch, code, *, cookies=None):
        if code != "123456":
            return AuthResult(success=False, message="Неверный код", challenge=ch)
        return AuthResult(success=True, message="ok", cookies={".AspNetCore.Cookies": "x"})

    monkeypatch.setattr(MireaAuth, "login", fake_login)
    monkeypatch.setattr(MireaAuth, "complete_2fa", fake_2fa)

    from mirea_connect import services

    seen = {}

    async def fake_grades(cookies):
        seen["cookies"] = dict(cookies)
        cookies["__brs_visiting_log_id"] = "log1"  # мутация должна сохраниться
        return {"subjects": [{"name": "Физика", "total": 77}]}

    async def fake_sdo(cookies, deadline_limit=50):
        return {"courses": [{"id": 1, "name": "Физика"}], "deadlines": [], "grades": []}

    monkeypatch.setattr(services, "grades", fake_grades)
    monkeypatch.setattr(services, "sdo_overview", fake_sdo)

    cid = _register(client)
    verifier, pk = _pkce()
    req = _start(client, cid, pk)
    r = client.post("/login", data={"req": req, "action": "login", "username": "Me@edu.mirea.ru", "password": "secret"})
    assert r.status_code == 200 and "Код подтверждения" in r.text
    r = client.post("/login/otp", data={"req": req, "code": "000000"})
    assert r.status_code == 200 and "Неверный код" in r.text
    r = client.post("/login/otp", data={"req": req, "code": "123456"})
    assert r.status_code == 302
    tok = _token(client, cid, r.headers["location"], verifier)

    m = Mcp(client, tok["access_token"])
    err, text = m.call("my_grades", {})
    assert not err and "Физика" in text
    assert seen["cookies"][".AspNetCore.Cookies"] == "x"
    err, text = m.call("my_sdo", {})
    assert not err and "Физика" in text
    err, text = m.call("whoami", {})
    assert "me@edu.mirea.ru" in text.lower() and '"personal_tools": true' in text
    m.call("my_grades", {})
    assert seen["cookies"]["__brs_visiting_log_id"] == "log1"

    err, text = m.call("delete_my_data", {})
    assert not err
    r = client.post("/mcp", headers=m.h, json={"jsonrpc": "2.0", "id": 99, "method": "tools/list"})
    assert r.status_code == 401


def test_deny_and_stale_req(client):
    cid = _register(client)
    _, pk = _pkce()
    req = _start(client, cid, pk)
    r = client.post("/login", data={"req": req, "action": "deny"})
    assert r.status_code == 302 and "error=access_denied" in r.headers["location"]
    r = client.post("/login", data={"req": req, "action": "guest"})
    assert r.status_code == 400
    assert client.get("/login?req=nope").status_code == 400


def test_session_blob_is_encrypted(client, monkeypatch, tmp_path):
    async def ok_login(self, u, p):
        return AuthResult(success=True, message="ok", cookies={"SECRETCOOKIE": "v"})

    monkeypatch.setattr(MireaAuth, "login", ok_login)
    cid = _register(client)
    _, pk = _pkce()
    req = _start(client, cid, pk)
    r = client.post("/login", data={"req": req, "action": "login", "username": "a@edu.mirea.ru", "password": "pw-123"})
    assert r.status_code == 302
    raw = (tmp_path / "mirea-connect.sqlite3").read_bytes()
    assert b"SECRETCOOKIE" not in raw and b"pw-123" not in raw
