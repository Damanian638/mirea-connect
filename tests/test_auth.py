"""Регрессия PAR: код и correlation cookies принадлежат серверу Пульса."""

from __future__ import annotations

import secrets
import struct
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pymirea import AuthResult, Config, _settings, configure

from mirea_connect.auth import MireaAuth
from mirea_connect.pulse import GET_ME_URL, MireaGrades, verify_pulse_session

PULSE = "https://pulse.mirea.ru"
SSO = "https://sso.mirea.ru/realms/mirea"
ACTION = f"{SSO}/login-actions/authenticate?session_code=test&execution=password"
OTP_ACTION = f"{SSO}/login-actions/authenticate?session_code=test&execution=otp"
CALLBACK_PATH = f"{PULSE}/api/mireaauth"
CALLBACK = f"{CALLBACK_PATH}?code=server-owned&state=correlation"


def _grpc_response(message=b"\x0a\x08\x0a\x06\x0a\x04user", status="0"):
    trailers = f"grpc-status: {status}\r\n".encode()
    return httpx.Response(
        200,
        content=struct.pack(">BI", 0, len(message)) + message + struct.pack(">BI", 128, len(trailers)) + trailers,
        headers={"Content-Type": "application/grpc-web+proto"},
    )


def _form_post(action=CALLBACK_PATH, code="server-owned", state="correlation"):
    import html

    return (
        f'<form method="post" action="{html.escape(action)}">'
        f'<input type="hidden" name="code" value="{html.escape(code)}">'
        f'<input type="hidden" name="state" value="{html.escape(state)}">'
        '<input type="hidden" name="session_state" value="session">'
        '<noscript><button>Continue</button></noscript></form>'
        '<script>document.forms[0].submit();</script>'
    )


@pytest.fixture(autouse=True)
def configured():
    previous = _settings._cfg
    configure(Config(session_keys=secrets.token_urlsafe(32)))
    yield
    _settings._cfg = previous


def _page(action: str, **context) -> str:
    import json

    return "<script>const kcContext = " + json.dumps({"url": {"loginAction": action}, **context}) + ";</script>"


@pytest.mark.parametrize("otp", [False, True])
@pytest.mark.parametrize("session_created", [False, True])
@pytest.mark.parametrize("response_mode", ["query", "form_post"])
@pytest.mark.parametrize("cookie_name", ["Pulse.Auth.Cookie", ".AspNetCore.Cookies"])
async def test_par_login_preserves_correlation_and_leaves_code_to_pulse(otp, session_created, response_mode, cookie_name):
    with respx.mock(assert_all_called=False) as router:
        start = router.get(f"{PULSE}/api/auth/login", params={"redirectUri": "/"}).respond(
            302,
            headers={
                "Location": f"{SSO}/protocol/openid-connect/auth?client_id=attendance-app&request_uri=urn:test:par",
                "Set-Cookie": ".AspNetCore.Correlation.test=nonce; Path=/; Secure; HttpOnly",
            },
        )
        par = router.get(f"{SSO}/protocol/openid-connect/auth").respond(200, text=_page(ACTION))

        def completed():
            if response_mode == "form_post":
                return httpx.Response(200, text=_form_post())
            return httpx.Response(302, headers={"Location": CALLBACK})

        def authenticate(request):
            assert parse_qs(request.content.decode()) == {"username": ["me@edu.mirea.ru"], "password": ["test"]}
            if otp:
                return httpx.Response(200, text=_page(OTP_ACTION, pageId="email-code-form"))
            return completed()

        password = router.post(ACTION).mock(side_effect=authenticate)

        def confirm(request):
            assert parse_qs(request.content.decode()) == {"emailCode": ["123456"]}
            return completed()

        code = router.post(OTP_ACTION).mock(side_effect=confirm)

        def callback(request):
            assert ".AspNetCore.Correlation.test=nonce" in request.headers["cookie"]
            if response_mode == "form_post":
                assert parse_qs(request.content.decode()) == {
                    "code": ["server-owned"], "state": ["correlation"], "session_state": ["session"]
                }
            headers = {"Location": "/"}
            if session_created:
                headers["Set-Cookie"] = f"{cookie_name}=pulse-session; Path=/; Secure; HttpOnly"
            return httpx.Response(302, headers=headers)

        callback_route = (
            router.post(CALLBACK_PATH) if response_mode == "form_post" else router.get(CALLBACK)
        ).mock(side_effect=callback)
        router.get(f"{PULSE}/").respond(200, text="Pulse app")
        check = router.post(GET_ME_URL).mock(return_value=_grpc_response())
        # Этот endpoint не должен вызываться: код обменивает сам Пульс.
        token = router.post(f"{SSO}/protocol/openid-connect/token").respond(400)
        auth = MireaAuth()
        try:
            result = await auth.login("me@edu.mirea.ru", "test")
            if otp:
                assert result.challenge and result.challenge.kind == "email_code"
                result = await auth.complete_2fa(result.challenge, "123456")
            assert result.success is session_created
            if session_created:
                assert result.cookies[cookie_name] == "pulse-session"
            else:
                assert "сессию Пульса" in result.message
        finally:
            await auth.close()

        assert start.call_count == par.call_count == password.call_count == callback_route.call_count == 1
        assert code.call_count == int(otp)
        assert not token.called
        assert check.call_count == int(session_created)


@pytest.mark.parametrize(
    "action,code,state",
    [
        ("https://example.com/api/mireaauth", "code", "state"),
        (f"{PULSE}/other", "code", "state"),
        (CALLBACK_PATH, "", "state"),
        (CALLBACK_PATH, "code", ""),
    ],
)
async def test_form_post_only_submits_complete_callback_to_pulse(action, code, state):
    with respx.mock() as router:
        router.post(OTP_ACTION).respond(200, text=_form_post(action, code, state))
        auth = MireaAuth()
        try:
            response = await auth.client.post(OTP_ACTION, data={"emailCode": "123456"})
        finally:
            await auth.close()
        assert response.url.host == "sso.mirea.ru"
        assert len(router.calls) == 1


async def test_sso_error_does_not_submit_credentials():
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{PULSE}/api/auth/login").respond(503)
        auth = MireaAuth()
        try:
            result = await auth.login("me@edu.mirea.ru", "test")
        finally:
            await auth.close()
        assert not result.success
        assert all(call.request.method == "GET" for call in router.calls)


async def test_existing_sso_session_completes_form_post_from_get_without_password():
    with respx.mock() as router:
        router.get(f"{PULSE}/api/auth/login").respond(
            302,
            headers={
                "Location": f"{SSO}/protocol/openid-connect/auth?request_uri=urn:test:par",
                "Set-Cookie": ".AspNetCore.Correlation.test=opaque-nonce; Path=/; Secure",
            },
        )
        router.get(f"{SSO}/protocol/openid-connect/auth").respond(200, text=_form_post())

        def callback(request):
            assert ".AspNetCore.Correlation.test=opaque-nonce" in request.headers["cookie"]
            return httpx.Response(302, headers={
                "Location": "/",
                "Set-Cookie": "Pulse.Auth.Cookie=opaque-secret-cookie; Path=/; Secure",
            })

        router.post(CALLBACK_PATH).mock(side_effect=callback)
        router.get(f"{PULSE}/").respond(200, text="Pulse app")
        router.post(GET_ME_URL).mock(return_value=_grpc_response())
        auth = MireaAuth()
        try:
            result = await auth.login("me@edu.mirea.ru", "secret-password")
            assert result.success
            import json

            trace = json.dumps(auth.client.trace)
            assert not any(secret in trace for secret in (
                "opaque-secret-cookie", "opaque-nonce", "secret-password", "server-owned", "me@edu.mirea.ru"
            ))
            assert all(call.request.url.host == "pulse.mirea.ru" for call in router.calls if call.request.method == "POST")
        finally:
            await auth.close()


async def test_recovers_pulse_from_keycloak_session_without_another_otp():
    with respx.mock() as router:
        router.get(f"{PULSE}/api/auth/login").respond(302, headers={"Location": f"{SSO}/protocol/openid-connect/auth"})
        router.get(f"{SSO}/protocol/openid-connect/auth").respond(200, text=_form_post())
        router.post(CALLBACK_PATH).respond(302, headers={
            "Location": "/", "Set-Cookie": "Pulse.Auth.Cookie=session; Path=/; Secure"
        })
        router.get(f"{PULSE}/").respond(200, text="Pulse app")
        router.post(GET_ME_URL).mock(return_value=_grpc_response())
        auth = MireaAuth()
        auth.client.cookies.set("KEYCLOAK_SESSION", "sso-session", domain="sso.mirea.ru")
        try:
            result = await auth._require_session(AuthResult(success=True, message="SSO accepted"))
            assert result.success
            assert result.cookies["Pulse.Auth.Cookie"] == "session"
        finally:
            await auth.close()


@pytest.mark.parametrize("status,content_type", [(401, "application/json"), (200, "text/html")])
async def test_cookie_is_not_saved_without_authenticated_api_response(status, content_type):
    with respx.mock() as router:
        router.post(GET_ME_URL).respond(status, headers={"Content-Type": content_type})
        auth = MireaAuth()
        auth.client.cookies.set(".AspNetCore.Cookies", "expired-session", domain="pulse.mirea.ru")
        try:
            result = await auth._require_session(AuthResult(success=True, message="callback completed"))
            assert not result.success
            assert result.cookies is None
        finally:
            await auth.close()


@pytest.mark.parametrize(
    "message,status,authorized",
    [
        (b"\x0a\x08\x0a\x06\x0a\x04user", "0", True),
        (b"\x12\x02\x0a\x00", "0", False),
        (b"\x0a\x08\x0a\x06\x0a\x04user", "16", False),
        (b"\x0a\x08\x0a", "0", False),
        (b"", "0", False),
    ],
)
async def test_session_verification_requires_authorized_grpc_result(message, status, authorized):
    with respx.mock() as router:
        router.post(GET_ME_URL).mock(return_value=_grpc_response(message, status))
        async with httpx.AsyncClient() as client:
            assert await verify_pulse_session(client) is authorized


@pytest.mark.parametrize("cookie_name", ["Pulse.Auth.Cookie", ".AspNetCore.Cookies"])
async def test_grades_accepts_session_cookie_and_chunks_without_restarting_sso(cookie_name):
    cookies = {cookie_name: "chunks-2", cookie_name + "C1": "part-one", cookie_name + "C2": "part-two"}
    with respx.mock() as router:
        def get_me(request):
            for name, value in cookies.items():
                assert f"{name}={value}" in request.headers["cookie"]
            return _grpc_response()

        router.post(GET_ME_URL).mock(side_effect=get_me)
        api = MireaGrades(cookies)
        try:
            assert await api._ensure_aspnet_cookie() == (True, None)
            assert await verify_pulse_session(api.client)
        finally:
            await api.close()
        assert len(router.calls) == 1
