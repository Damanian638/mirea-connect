"""Логи без секретов, ограничение подбора пароля, OAuth: redirect_uri, PKCE, коды, отзыв доступа."""

from __future__ import annotations

import io
import json
import logging
import time
from urllib.parse import parse_qs, urlparse

import pytest
from pymirea.auth import AuthResult
from test_flow import REDIRECT, Mcp, _pkce, _register, _start, _token, client  # noqa: F401

from mirea_connect.auth import MireaAuth

SSO = ("https://sso.mirea.ru/realms/mirea/login-actions/authenticate"
       "?session_code=SECRET-SC&execution=e1&client_id=attendance-app&tab_id=T1")


# --- логи ---


@pytest.fixture
def logstream():
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.getLogger("pymirea").level)
    from mirea_connect import logs

    stream = io.StringIO()
    logs.setup_logging(logging.INFO, stream=stream)
    yield stream
    root.handlers[:], root.level = saved[0], saved[1]
    logging.getLogger("pymirea").setLevel(saved[2])


def test_sso_urls_and_codes_are_redacted_in_logs(logstream):
    logging.getLogger("pymirea.auth").info(f"Final URL: {SSO}")
    logging.getLogger("pymirea.grades").info("BRS auth bootstrap: 200 final=https://pulse.mirea.ru/signin-oidc?code=ONE-TIME&state=ST")
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "1.2.3.4", "GET", "/login?req=PENDING-REQ", "1.1", 200)
    try:
        raise RuntimeError(f"Client error '400 Bad Request' for url '{SSO}'")
    except RuntimeError:
        logging.getLogger("mirea_connect.web").exception("login failed")
    out = logstream.getvalue()
    for secret in ("SECRET-SC", "T1", "ONE-TIME", "PENDING-REQ", "=ST"):
        assert secret not in out, secret
    assert "sso.mirea.ru/realms/mirea/login-actions/authenticate" in out  # путь для диагностики остаётся
    assert "session_code=" in out and "login failed" in out and "RuntimeError" in out


def test_logging_goes_to_given_stream_not_stdout(capsys):
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    from mirea_connect import logs

    try:
        logs.setup_logging(logging.WARNING)
        logging.getLogger("x").warning("hello")
        cap = capsys.readouterr()
        assert cap.out == "" and "hello" in cap.err
    finally:
        root.handlers[:], root.level = saved


@pytest.mark.parametrize(
    "text, expected",
    [
        ("GET /authorize?client_id=a&state=s&code_challenge=c", "GET /authorize?client_id=…&state=…&code_challenge=…"),
        ("нет параметров https://pulse.mirea.ru/", "нет параметров https://pulse.mirea.ru/"),
        ("token=abc без адреса", "token=abc без адреса"),
    ],
)
def test_redact(text, expected):
    from mirea_connect import logs

    assert logs.redact(text) == expected


# --- подбор пароля ---


def test_login_attempts_limited_per_account_even_with_spoofed_ip(client, monkeypatch):  # noqa: F811
    calls = []

    async def fake_login(self, username, password):
        calls.append(username)
        return AuthResult(success=False, message="Неверный логин или пароль")

    monkeypatch.setattr(MireaAuth, "login", fake_login)
    cid = _register(client)
    blocked = 0
    for n in range(4):  # 4 запроса авторизации × 5 попыток, каждый раз «новый IP»
        _, pk = _pkce()
        req = _start(client, cid, pk)
        for k in range(5):
            r = client.post("/login", headers={"X-Forwarded-For": f"10.0.{n}.{k}"},
                            data={"req": req, "action": "login", "username": "Victim@edu.mirea.ru", "password": f"p{n}{k}"})
            blocked += "Слишком много попыток" in r.text
    assert len(calls) <= 10 and blocked >= 10


# --- OAuth ---


def _authorize_guest(c, cid, challenge):
    req = _start(c, cid, challenge)
    r = c.post("/login", data={"req": req, "action": "guest"})
    assert r.status_code == 302
    return r.headers["location"]


def _exchange(c, cid, location, verifier, redirect=REDIRECT):
    code = parse_qs(urlparse(location).query)["code"][0]
    return c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                  "client_id": cid, "code_verifier": verifier})


def test_unregistered_redirect_uri_is_rejected_without_redirect(client):  # noqa: F811
    cid = _register(client)
    _, ch = _pkce()
    r = client.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": "https://evil.example/cb",
                                         "code_challenge": ch, "code_challenge_method": "S256", "state": "st"})
    assert r.status_code == 400
    assert "evil.example" not in r.headers.get("location", "")


def test_state_is_returned_unchanged(client):  # noqa: F811
    cid = _register(client)
    _, ch = _pkce()
    loc = _authorize_guest(client, cid, ch)
    assert loc.startswith(REDIRECT) and parse_qs(urlparse(loc).query)["state"] == ["st"]


def test_wrong_pkce_verifier_rejected(client):  # noqa: F811
    cid = _register(client)
    _, ch = _pkce()
    other, _ = _pkce()
    r = _exchange(client, cid, _authorize_guest(client, cid, ch), other)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_code_cannot_be_reused(client):  # noqa: F811
    cid = _register(client)
    v, ch = _pkce()
    loc = _authorize_guest(client, cid, ch)
    assert _exchange(client, cid, loc, v).status_code == 200
    r = _exchange(client, cid, loc, v)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_code_bound_to_redirect_uri_and_client(client):  # noqa: F811
    cid = _register(client)
    other_cid = _register(client)
    v, ch = _pkce()
    loc = _authorize_guest(client, cid, ch)
    assert _exchange(client, other_cid, loc, v).status_code == 400
    assert _exchange(client, cid, loc, v, redirect="http://localhost:9999/other").status_code == 400


def test_expired_code_rejected(client, monkeypatch):  # noqa: F811
    cid = _register(client)
    v, ch = _pkce()
    loc = _authorize_guest(client, cid, ch)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 6 * 60)
    r = _exchange(client, cid, loc, v)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_expired_pending_login_page(client, monkeypatch):  # noqa: F811
    cid = _register(client)
    _, ch = _pkce()
    req = _start(client, cid, ch)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 16 * 60)
    assert client.get(f"/login?req={req}").status_code == 400
    assert client.post("/login", data={"req": req, "action": "guest"}).status_code == 400


def test_users_are_isolated_and_delete_my_data_revokes_only_own_access(client, monkeypatch):  # noqa: F811
    from mirea_connect import services

    async def fake_login(self, username, password):
        return AuthResult(success=True, message="ok", cookies={"who": username})

    async def fake_grades(cookies):
        return {"subjects": [{"name": cookies["who"]}]}

    monkeypatch.setattr(MireaAuth, "login", fake_login)
    monkeypatch.setattr(services, "grades", fake_grades)

    def connect(login):
        cid = _register(client)
        v, ch = _pkce()
        req = _start(client, cid, ch)
        r = client.post("/login", data={"req": req, "action": "login", "username": login, "password": "pw"})
        tok = _token(client, cid, r.headers["location"], v)
        return Mcp(client, tok["access_token"]), tok

    alice, alice_tok = connect("alice@edu.mirea.ru")
    bob, _ = connect("bob@edu.mirea.ru")
    assert "alice@" in alice.call("my_grades", {})[1] and "bob@" not in alice.call("my_grades", {})[1]
    assert "bob@" in bob.call("my_grades", {})[1]

    assert not alice.call("delete_my_data", {})[0]
    r = client.post("/mcp", headers=alice.h, json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    assert r.status_code == 401
    r = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": alice_tok["refresh_token"],
                                    "client_id": "x"})
    assert r.status_code in (400, 401)
    err, text = bob.call("my_grades", {})  # чужой доступ не затронут
    assert not err and "bob@" in text
    assert json.loads(bob.call("whoami", {})[1])["personal_tools"] is True
