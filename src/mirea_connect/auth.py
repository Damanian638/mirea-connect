"""Вход через Пульс: PAR и обмен OAuth-кода выполняет сервер университета."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from pymirea.auth import AuthResult
from pymirea.auth import MireaAuth as _MireaAuth

from .pulse import PULSE_SESSION_COOKIES, has_pulse_session, verify_pulse_session


class _PulseLoginClient:
    """Сохраняет HTTP-клиент pymirea, начиная авторизацию через Пульс.

    Прямой запрос attendance-app к SSO больше не принимается: нужен PAR,
    который создаёт /api/auth/login вместе с correlation/nonce cookies.
    Остальные запросы, включая callback Пульса, используют тот же клиент.
    """

    def __init__(self, client, auth_url: str, login_url: str) -> None:
        self._client = client
        self._auth_url = auth_url
        self._login_url = login_url
        self._trace: list[dict] = []

    @property
    def trace(self) -> list[dict]:
        entries = self._trace
        names = set()
        for cookie in self._client.cookies.jar:
            name = cookie.name
            for prefix in (".AspNetCore.Correlation.", ".AspNetCore.OpenIdConnect.Nonce."):
                if name.startswith(prefix):
                    name = prefix + "*"
            names.add(name)
        return [*entries, {"cookie_names": sorted(names)}]

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def get(self, url, **kwargs):
        if str(url) == self._auth_url and (kwargs.get("params") or {}).get("response_type") == "code":
            url = self._login_url
            kwargs["params"] = {"redirectUri": "/"}
        return await self._complete_form_post(await self._client.get(url, **kwargs))

    async def post(self, url, **kwargs):
        return await self._complete_form_post(await self._client.post(url, **kwargs))

    def _record(self, response):
        # Только метаданные: без URL-параметров, тел форм и значений cookies.
        entries = self._trace
        for item in [*response.history, response]:
            entries.append({
                "method": item.request.method,
                "host": item.url.host,
                "path": item.url.path,
                "status": item.status_code,
                "content_type": item.headers.get("content-type", "").split(";")[0],
                "pulse_session": any(c.name in PULSE_SESSION_COOKIES for c in item.cookies.jar),
                "keycloak_session": any(c.name in {"KEYCLOAK_IDENTITY", "KEYCLOAK_SESSION"} for c in item.cookies.jar),
            })
        entries[:] = entries[-40:]

    async def _complete_form_post(self, response):
        self._record(response)
        if response.status_code != 200 or response.url.host != urlsplit(self._auth_url).hostname:
            return response

        # PAR Пульса запрашивает response_mode=form_post. Keycloak возвращает
        # HTML с автоподачей code/state вместо HTTP Location. Отправляем эту
        # форму тем же клиентом, сохраняя nonce и correlation cookies.
        pulse = urlsplit(self._login_url)
        for form in BeautifulSoup(response.text, "html.parser").find_all("form"):
            if str(form.get("method", "get")).lower() != "post":
                continue
            action = urljoin(str(response.url), str(form.get("action", "")))
            target = urlsplit(action)
            if (
                (target.scheme, target.netloc) != (pulse.scheme, pulse.netloc)
                or target.path not in {"/api/mireaauth", "/signin-oidc"}
            ):
                continue
            fields = {
                str(inp["name"]): str(inp.get("value", ""))
                for inp in form.find_all("input", attrs={"name": True})
                if str(inp.get("type", "")).lower() == "hidden"
            }
            if not fields.get("code") or not fields.get("state"):
                continue
            result = await self._client.post(
                action,
                data=fields,
                headers={"Referer": str(response.url)},
                follow_redirects=True,
            )
            self._record(result)
            return result
        return response


class MireaAuth(_MireaAuth):
    """Совместимость pymirea с обязательным PAR у attendance-app."""

    def __init__(self):
        super().__init__()
        self.client = _PulseLoginClient(
            self.client,
            f"{self.SSO_URL}/realms/mirea/protocol/openid-connect/auth",
            f"{self.ATTENDANCE_URL}/api/auth/login",
        )

    async def _exchange_code_for_token(self, code, *, code_verifier, redirect_uri):
        # Код принадлежит серверному потоку Пульса. Его должен обменять
        # /signin-oidc, иначе можно погасить одноразовый код до callback.
        return None

    async def _require_session(self, result: AuthResult) -> AuthResult:
        if result.challenge:
            return result
        cookies = dict(self.client.cookies)
        if not has_pulse_session(cookies) and any(
            cookies.get(name) for name in ("KEYCLOAK_IDENTITY", "KEYCLOAK_SESSION")
        ):
            # SSO уже принял код, но промежуточная required-action могла
            # прервать callback. Восстанавливаем Пульс той же SSO-сессией.
            await self.client.get(f"{self.ATTENDANCE_URL}/api/auth/login", params={"redirectUri": "/"})
            cookies = dict(self.client.cookies)
        if has_pulse_session(cookies):
            if await verify_pulse_session(self.client):
                return AuthResult(success=True, message="Авторизация успешна", cookies=dict(self.client.cookies))
            return AuthResult(success=False, message="Пульс не подтвердил сессию. Войдите заново.")
        if result.success:
            return AuthResult(success=False, message="Не удалось получить сессию Пульса. Попробуйте войти заново.")
        return result

    async def login(self, username: str, password: str) -> AuthResult:
        return await self._require_session(await super().login(username, password))

    async def submit_otp(self, challenge, otp_code: str, *, cookies=None) -> AuthResult:
        return await self._require_session(await super().submit_otp(challenge, otp_code, cookies=cookies))
