"""HTML-страницы: главная, вход в МИРЭА (логин → OTP), согласие."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from pymirea.auth import AuthChallenge
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .auth import MireaAuth
from .config import Settings
from .oauth import MireaOAuthProvider, new_guest_id, user_id_for_login
from .sessions import SessionVault

logger = logging.getLogger(__name__)

OTP_TTL_S = 10 * 60
MAX_ATTEMPTS_PER_REQ = 5
MAX_LOGINS_PER_IP_PER_10MIN = 10
# IP берётся из заголовков прокси и может быть подделан, поэтому подбор пароля
# к одной учётной записи ограничен отдельно — сколько бы запросов авторизации ни создали.
MAX_LOGINS_PER_ACCOUNT_PER_10MIN = 10

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#14161a;--muted:#5d6470;--line:#dfe3e8;--accent:#1a56db;--accent-fg:#fff;--err:#b42318;--errbg:#fdecea}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--fg:#e8eaed;--muted:#9aa1ad;--line:#2a2f37;--accent:#5b8cff;--accent-fg:#0b0d10;--err:#ff8a80;--errbg:#3a1d1b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:440px;margin:0 auto;padding:40px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px}
h1{font-size:22px;margin:0 0 4px}p{margin:8px 0}.muted{color:var(--muted);font-size:14px}
label{display:block;font-size:14px;margin:16px 0 6px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
button{width:100%;margin-top:20px;padding:11px;border:0;border-radius:8px;background:var(--accent);color:var(--accent-fg);font:inherit;font-weight:600;cursor:pointer}
button.secondary{background:transparent;color:var(--fg);border:1px solid var(--line);margin-top:10px}
.err{background:var(--errbg);color:var(--err);padding:10px 12px;border-radius:8px;font-size:14px;margin-top:12px}
code{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:2px 6px;font-size:14px;word-break:break-all}
ul{padding-left:20px;margin:8px 0}li{margin:4px 0}
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<meta name="referrer" content="no-referrer"><style>{_CSS}</style></head>
<body><main>{body}</main></body></html>"""
    return HTMLResponse(
        doc,
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
        },
    )


def _err(msg: str | None) -> str:
    return f'<div class="err">{html.escape(msg)}</div>' if msg else ""


@dataclass
class _OtpState:
    auth: MireaAuth
    challenge: AuthChallenge
    login: str
    created: float = field(default_factory=time.time)


class LoginFlow:
    def __init__(self, provider: MireaOAuthProvider, vault: SessionVault, settings: Settings) -> None:
        self.provider = provider
        self.vault = vault
        self.settings = settings
        self._otp: dict[str, _OtpState] = {}
        self._attempts: dict[str, int] = defaultdict(int)
        self._ip_hits: dict[str, deque] = defaultdict(deque)
        self._account_hits: dict[str, deque] = defaultdict(deque)
        self._lock = asyncio.Lock()

    # --- helpers ---

    def _client_ip(self, request: Request) -> str:
        return (
            request.headers.get("cf-connecting-ip")
            or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            or (request.client.host if request.client else "?")
        )

    def _rate_limited(self, ip: str, account: str = "") -> bool:
        now = time.time()
        checks = [(self._ip_hits, ip, MAX_LOGINS_PER_IP_PER_10MIN)]
        if account:
            checks.append((self._account_hits, account.strip().casefold(), MAX_LOGINS_PER_ACCOUNT_PER_10MIN))
        for hits, key, _ in checks:
            q = hits[key]
            while q and now - q[0] > 600:
                q.popleft()
            if not q:
                hits.pop(key, None)  # не копить пустые записи
        if any(len(hits.get(key, ())) >= limit for hits, key, limit in checks):
            return True
        for hits, key, _ in checks:
            hits[key].append(now)
        return False

    async def _drop_otp(self, req: str) -> None:
        st = self._otp.pop(req, None)
        if st:
            try:
                await st.auth.close()
            except Exception:
                pass

    async def _gc(self) -> None:
        now = time.time()
        for req in [r for r, st in self._otp.items() if now - st.created > OTP_TTL_S]:
            await self._drop_otp(req)

    def _finish(self, req: str, user_id: str) -> Response:
        self._attempts.pop(req, None)
        url = self.provider.complete_authorization(req, user_id)
        return RedirectResponse(url, status_code=302)

    # --- pages ---

    def _login_form(self, req: str, info: dict, error: str | None = None, login: str = "") -> HTMLResponse:
        client = html.escape(info.get("client_name") or "MCP-клиент")
        guest = ""
        if self.settings.allow_guest:
            guest = '<button class="secondary" name="action" value="guest" formnovalidate>Без входа — только публичное расписание</button>'
        body = f"""<div class="card">
<h1>Вход в МИРЭА</h1>
<p class="muted">{client} просит доступ к вашему Пульсу: расписание, баллы БРС, посещаемость, турникеты. Только чтение.</p>
{_err(error)}
<form method="post" action="/login" autocomplete="on">
<input type="hidden" name="req" value="{html.escape(req)}">
<label for="u">Логин</label><input id="u" name="username" type="email" placeholder="ivanov.i.i@edu.mirea.ru" value="{html.escape(login)}" required autocomplete="username">
<label for="p">Пароль</label><input id="p" name="password" type="password" required autocomplete="current-password">
<button name="action" value="login">Войти</button>
{guest}
<button class="secondary" name="action" value="deny" formnovalidate>Отмена</button>
</form>
<p class="muted">Пароль используется один раз для входа через sso.mirea.ru и не сохраняется. На сервере остаётся только зашифрованная сессия Пульса — её можно удалить инструментом <code>delete_my_data</code>.</p>
</div>"""
        return _page("Вход в МИРЭА", body)

    def _otp_form(self, req: str, error: str | None = None) -> HTMLResponse:
        body = f"""<div class="card">
<h1>Код подтверждения</h1>
<p class="muted">МИРЭА отправила код на вашу университетскую почту.</p>
{_err(error)}
<form method="post" action="/login/otp">
<input type="hidden" name="req" value="{html.escape(req)}">
<label for="c">Код</label><input id="c" name="code" inputmode="numeric" autocomplete="one-time-code" required autofocus>
<button>Подтвердить</button>
</form></div>"""
        return _page("Код подтверждения", body)

    def _expired(self) -> HTMLResponse:
        return _page(
            "Ссылка устарела",
            '<div class="card"><h1>Ссылка устарела</h1><p>Начните подключение коннектора заново в своём MCP-клиенте.</p></div>',
            status=400,
        )

    async def get_login(self, request: Request) -> Response:
        req = request.query_params.get("req", "")
        info = self.provider.describe_pending(req)
        if not info:
            return self._expired()
        return self._login_form(req, info)

    async def post_login(self, request: Request) -> Response:
        form = await request.form()
        req = str(form.get("req", ""))
        info = self.provider.describe_pending(req)
        if not info:
            return self._expired()
        action = str(form.get("action", "login"))
        if action == "deny":
            await self._drop_otp(req)
            url = self.provider.deny_authorization(req)
            return RedirectResponse(url, status_code=302) if url else self._expired()
        if action == "guest":
            if not self.settings.allow_guest:
                return self._login_form(req, info, "Гостевой режим отключён на этом сервере.")
            return self._finish(req, new_guest_id())

        login = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        if not login or not password:
            return self._login_form(req, info, "Введите логин и пароль.", login)
        if self._rate_limited(self._client_ip(request), login):
            return self._login_form(req, info, "Слишком много попыток. Подождите 10 минут.", login)
        self._attempts[req] += 1
        if self._attempts[req] > MAX_ATTEMPTS_PER_REQ:
            return self._login_form(req, info, "Слишком много попыток. Начните подключение заново.", login)

        await self._gc()
        await self._drop_otp(req)
        auth = MireaAuth()
        try:
            result = await auth.login(login, password)
        except Exception:
            logger.exception("login failed")
            await auth.close()
            return self._login_form(req, info, "МИРЭА не ответила. Попробуйте позже.", login)
        if result.challenge:
            self._otp[req] = _OtpState(auth=auth, challenge=result.challenge, login=login)
            return self._otp_form(req)
        await auth.close()
        if not result.success or not result.cookies:
            return self._login_form(req, info, result.message or "Не удалось войти.", login)
        user_id = user_id_for_login(login)
        self.vault.store(user_id, login, result.cookies)
        return self._finish(req, user_id)

    async def post_otp(self, request: Request) -> Response:
        form = await request.form()
        req = str(form.get("req", ""))
        if not self.provider.describe_pending(req):
            await self._drop_otp(req)
            return self._expired()
        st = self._otp.get(req)
        if not st or time.time() - st.created > OTP_TTL_S:
            await self._drop_otp(req)
            info = self.provider.describe_pending(req) or {}
            return self._login_form(req, info, "Время на ввод кода вышло, войдите ещё раз.")
        self._attempts[req] += 1
        if self._attempts[req] > MAX_ATTEMPTS_PER_REQ + 3:
            await self._drop_otp(req)
            return self._expired()
        code = str(form.get("code", "")).strip()
        try:
            result = await st.auth.complete_2fa(st.challenge, code)
        except Exception:
            logger.exception("otp failed")
            return self._otp_form(req, "МИРЭА не ответила. Попробуйте ещё раз.")
        if result.challenge:  # неверный код — Keycloak показывает форму снова
            st.challenge = result.challenge
            return self._otp_form(req, result.message or "Неверный код.")
        if not result.success or not result.cookies:
            return self._otp_form(req, result.message or "Неверный код.")
        await self._drop_otp(req)
        user_id = user_id_for_login(st.login)
        self.vault.store(user_id, st.login, result.cookies)
        return self._finish(req, user_id)

    # --- misc ---

    async def index(self, request: Request) -> Response:
        url = html.escape(self.settings.mcp_url)
        body = f"""<div class="card">
<h1>MIREA Connect</h1>
<p>MCP-коннектор к Пульсу и расписанию РТУ МИРЭА для Claude, Cursor и других MCP-клиентов.</p>
<p><b>Адрес коннектора:</b><br><code>{url}</code></p>
<ul>
<li><b>claude.ai</b>: Настройки → Коннекторы → Добавить свой коннектор → вставьте адрес.</li>
<li><b>Claude Code</b>: <code>claude mcp add --transport http mirea {url}</code></li>
<li><b>Cursor и другие</b>: добавьте HTTP MCP-сервер с этим адресом.</li>
</ul>
<p class="muted">При подключении откроется страница входа в МИРЭА. Можно войти без аккаунта — тогда доступно только публичное расписание.</p>
</div>"""
        return _page("MIREA Connect", body)

    async def health(self, request: Request) -> Response:
        return JSONResponse({"status": "ok"})
