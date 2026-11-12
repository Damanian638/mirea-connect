from __future__ import annotations

import secrets
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pymirea import Config, _settings, configure

from mirea_connect.sdo import MireaSDO

MOODLE = "https://online-edu.mirea.ru"
SSO = "https://sso.mirea.ru/realms/mirea/protocol/openid-connect/auth"


@pytest.fixture(autouse=True)
def configured():
    previous = _settings._cfg
    configure(Config(session_keys=secrets.token_urlsafe(32)))
    yield
    _settings._cfg = previous


def form(action=f"{MOODLE}/auth/oidc/", code="one-time-code", state="correlation"):
    return (
        f'<form method="post" action="{action}">'
        f'<input type="hidden" name="code" value="{code}">'
        f'<input type="hidden" name="state" value="{state}">'
        '<input type="submit" name="continue" value="Continue"></form>'
    )


@pytest.mark.parametrize("cached", [False, True])
async def test_form_post_finishes_moodle_login_and_recovers_expired_cache(cached):
    cookies = {"KEYCLOAK_IDENTITY": "sso-session", "Pulse.Auth.Cookie": "pulse-session"}
    if cached:
        cookies["__sdo_moodle_session"] = "expired"
    with respx.mock() as router:
        router.get(f"{MOODLE}/login/index.php").respond(200, text='"sesskey":"guest-key"')

        def dashboard(request):
            if "MoodleSession=authenticated" in request.headers.get("cookie", ""):
                return httpx.Response(200, text='M.cfg = {"sesskey": "private-key"};')
            return httpx.Response(302, headers={"Location": "/login/index.php"})

        router.get(f"{MOODLE}/my/").mock(side_effect=dashboard)
        start = router.get(f"{MOODLE}/auth/oidc/").respond(303, headers={
            "Location": SSO,
            "Set-Cookie": "MoodleSession=guest; Path=/; Secure",
        })
        router.get(SSO).respond(200, text=form())

        def callback(request):
            assert "MoodleSession=guest" in request.headers["cookie"]
            assert parse_qs(request.content.decode()) == {
                "code": ["one-time-code"], "state": ["correlation"]
            }
            return httpx.Response(302, headers={
                "Location": "/my/", "Set-Cookie": "MoodleSession=authenticated; Path=/; Secure"
            })

        post = router.post(f"{MOODLE}/auth/oidc/").mock(side_effect=callback)
        router.post(f"{MOODLE}/lib/ajax/service.php").respond(200, json=[{
            "data": {"courses": [{"id": 1, "fullname": "Physics"}]}
        }])
        api = MireaSDO(cookies)
        try:
            result = await api.get_courses()
            assert result.success
            assert result.courses[0].name == "Physics"
            assert cookies["__sdo_moodle_session"] == "authenticated"
            assert (await api.get_courses()).success
            assert start.call_count == post.call_count == 1
        finally:
            await api.close()


@pytest.mark.parametrize("action,code,state", [
    ("https://example.com/auth/oidc/", "code", "state"),
    (f"{MOODLE}/other", "code", "state"),
    (f"{MOODLE}/auth/oidc/", "", "state"),
    (f"{MOODLE}/auth/oidc/", "code", ""),
])
async def test_untrusted_or_incomplete_form_is_not_submitted(action, code, state):
    with respx.mock() as router:
        router.get(f"{MOODLE}/auth/oidc/").respond(303, headers={
            "Location": SSO, "Set-Cookie": "MoodleSession=guest; Path=/; Secure"
        })
        router.get(SSO).respond(200, text=form(action, code, state))
        router.get(f"{MOODLE}/my/").respond(302, headers={"Location": "/login/index.php"})
        router.get(f"{MOODLE}/login/index.php").respond(200, text='"sesskey":"guest-key"')
        api = MireaSDO({"KEYCLOAK_IDENTITY": "sso-session"})
        try:
            assert not await api.bootstrap_session()
            assert "__sdo_moodle_session" not in api.session_cookies
            assert all(call.request.method == "GET" for call in router.calls)
        finally:
            await api.close()
