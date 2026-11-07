"""Локальный режим: MCP по stdio, вход и сессия — только на устройстве пользователя.

Сессия хранится тем же способом, что и в CLI pymirea (``~/.config/pymirea``,
зашифровано, права 0600), поэтому сохранённую сессию могут читать оба CLI.
Для входа без терминала есть инструмент ``login``: он
поднимает страницу на ``127.0.0.1`` со случайным адресом и открывает браузер.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.mcpserver import MCPServer
from pymirea.auth import AuthChallenge
from pymirea.exceptions import MireaSessionExpired
from pymirea.store import NotLoggedIn, SessionStore
from pymirea.tokens import ensure_fresh_token
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from .auth import MireaAuth
from .logs import setup_logging
from .services import ServiceError
from .sessions import NoSession
from .tools import BASE_INSTRUCTIONS, LOCAL_ACTION, register_tools
from .web import _err, _page

logger = logging.getLogger(__name__)

LOGIN_PAGE_TTL_S = 15 * 60

INSTRUCTIONS = BASE_INSTRUCTIONS + (
    " Коннектор работает локально на устройстве пользователя. Если личные инструменты (my_*) сообщают, "
    "что входа нет или сессия истекла, вызови инструмент login: он откроет страницу входа МИРЭА "
    "в браузере на этом компьютере. Дай пользователю ссылку из ответа и подожди, пока он скажет, что вошёл."
)

RELOGIN_HINT = (
    "Вызови инструмент login — откроется страница входа МИРЭА на этом компьютере "
    "(или выполни в терминале `mirea-connect login`)."
)


class LocalSource:
    relogin_hint = RELOGIN_HINT

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def use(self) -> AsyncIterator[dict]:
        async with self._lock:
            try:
                stored = self.store.load()
            except NotLoggedIn as e:
                raise NoSession("Вход в МИРЭА не выполнен.") from e
            cookies = stored.cookies
            before = json.dumps(cookies, sort_keys=True, default=str)
            try:
                await ensure_fresh_token(cookies, buffer_s=60)
            except MireaSessionExpired as e:
                self.store.clear()
                raise NoSession("Сессия МИРЭА истекла.") from e
            try:
                yield cookies
            finally:
                if json.dumps(cookies, sort_keys=True, default=str) != before:
                    self.store.save(cookies)

    def whoami(self) -> dict:
        try:
            stored = self.store.load()
        except NotLoggedIn:
            return {"mode": "local", "logged_in": False, "personal_tools": False}
        return {"mode": "local", "logged_in": True, "login": stored.login, "personal_tools": True}

    def forget(self) -> None:
        self.store.clear()

    def mark_dead(self) -> None:
        self.store.clear()


# --- локальная страница входа ---


class _NoSignalServer:
    """uvicorn.Server без перехвата сигналов — их обрабатывает MCP-процесс."""

    def __new__(cls, config):
        import uvicorn

        class S(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self):
                yield

        return S(config)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _open_browser(url: str) -> bool:
    """Открыть ссылку, не допуская вывода браузера в stdout.

    ``webbrowser.open`` запускает xdg-open/браузер с унаследованным stdout, а в
    stdio-режиме это канал JSON-RPC: строка вроде «Opening in existing browser
    session.» ломает протокол. Поэтому все потоки дочернего процесса — в DEVNULL."""
    if sys.platform == "win32":
        os.startfile(url)  # type: ignore[attr-defined]  # ShellExecute, без консольного вывода
        return True
    candidates: list[list[str]] = []
    for entry in filter(None, os.environ.get("BROWSER", "").split(os.pathsep)):
        cmd = shlex.split(entry)
        candidates.append([a.replace("%s", url) for a in cmd] if "%s" in entry else [*cmd, url])
    if sys.platform == "darwin":
        candidates.append(["open", url])
    candidates += [["xdg-open", url], ["gio", "open", url]]
    for cmd in candidates:
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        try:
            subprocess.Popen(
                [exe, *cmd[1:]],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except OSError:
            continue
    return False


class LocalLogin:
    """Одноразовая страница входа на 127.0.0.1 со случайным секретом в пути."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.url: str | None = None
        self.done = asyncio.Event()
        self._secret = ""
        self._server = None
        self._task: asyncio.Task | None = None
        self._auth: MireaAuth | None = None
        self._challenge: AuthChallenge | None = None
        self._login = ""
        self._attempts = 0
        self._started = 0.0
        self._timers: list[asyncio.TimerHandle] = []

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _schedule_stop(self, delay: float) -> None:
        # Таймер привязан к этой странице: stop() отменяет его, и он не закроет следующую.
        loop = asyncio.get_running_loop()
        self._timers.append(loop.call_later(delay, lambda: asyncio.ensure_future(self.stop())))

    async def start(self) -> str:
        if self.running and self.url and time.time() - self._started < LOGIN_PAGE_TTL_S:
            return self.url
        await self.stop()
        import uvicorn

        self._secret = secrets.token_urlsafe(18)
        self._attempts = 0
        self.done = asyncio.Event()
        base = f"/{self._secret}"
        app = Starlette(
            routes=[
                Route(base, self._get, methods=["GET"]),
                Route(f"{base}/login", self._post_login, methods=["POST"]),
                Route(f"{base}/otp", self._post_otp, methods=["POST"]),
            ]
        )
        port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False, lifespan="off")
        server = self._server = _NoSignalServer(config)

        async def serve() -> None:
            # uvicorn при ошибке запуска (порт занят) вызывает sys.exit(); SystemExit из задачи
            # остановил бы event loop и весь MCP-процесс вместе с ним.
            try:
                await server.serve()
            except SystemExit:
                logger.warning("local login page failed to start")

        self._task = asyncio.create_task(serve())
        self._started = time.time()
        self._schedule_stop(LOGIN_PAGE_TTL_S)
        for _ in range(100):
            if getattr(server, "started", False) or self._task.done():
                break
            await asyncio.sleep(0.05)
        if not getattr(server, "started", False):
            await self.stop()
            raise ServiceError("Не удалось открыть страницу входа на этом компьютере. Повторите попытку "
                               "или выполните в терминале `mirea-connect login`.")
        self.url = f"http://127.0.0.1:{port}{base}"
        return self.url

    async def stop(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        if self._auth:
            with contextlib.suppress(Exception):
                await self._auth.close()
        self._auth = None
        self._challenge = None
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=3)
        self._server = None
        self._task = None
        self.url = None

    # pages

    def _form(self, error: str | None = None, login: str = "") -> HTMLResponse:
        body = f"""<div class="card">
<h1>Вход в МИРЭА</h1>
<p class="muted">Страница открыта коннектором MIREA Connect на этом компьютере. Логин и пароль уходят только на sso.mirea.ru.</p>
{_err(error)}
<form method="post" action="/{self._secret}/login">
<label for="u">Логин</label><input id="u" name="username" type="email" placeholder="ivanov.i.i@edu.mirea.ru" value="{html.escape(login)}" required autocomplete="username">
<label for="p">Пароль</label><input id="p" name="password" type="password" required autocomplete="current-password">
<button>Войти</button>
</form></div>"""
        return _page("Вход в МИРЭА", body)

    def _otp(self, error: str | None = None) -> HTMLResponse:
        body = f"""<div class="card">
<h1>Код подтверждения</h1>
<p class="muted">МИРЭА отправила код на вашу университетскую почту.</p>
{_err(error)}
<form method="post" action="/{self._secret}/otp">
<label for="c">Код</label><input id="c" name="code" inputmode="numeric" autocomplete="one-time-code" required autofocus>
<button>Подтвердить</button>
</form></div>"""
        return _page("Код подтверждения", body)

    def _ok(self) -> HTMLResponse:
        body = """<div class="card"><h1>Готово</h1>
<p>Вы вошли в МИРЭА. Сессия сохранена на этом компьютере. Вкладку можно закрыть и вернуться в чат.</p></div>"""
        return _page("Готово", body)

    def _finish(self, cookies: dict) -> Response:
        self.store.save(cookies, login=self._login)
        self.done.set()
        self._schedule_stop(2)  # дать браузеру получить страницу «Готово»
        return self._ok()

    async def _get(self, request: Request) -> Response:
        return self._form()

    async def _post_login(self, request: Request) -> Response:
        form = await request.form()
        login = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        if not login or not password:
            return self._form("Введите логин и пароль.", login)
        self._attempts += 1
        if self._attempts > 8:
            return self._form("Слишком много попыток. Вызовите login заново.", login)
        if self._auth:
            with contextlib.suppress(Exception):
                await self._auth.close()
        self._auth = MireaAuth()
        self._login = login
        try:
            result = await self._auth.login(login, password)
        except Exception:
            logger.exception("login failed")
            return self._form("МИРЭА не ответила. Попробуйте позже.", login)
        if result.challenge:
            self._challenge = result.challenge
            return self._otp()
        if not result.success or not result.cookies:
            return self._form(result.message or "Не удалось войти.", login)
        return self._finish(result.cookies)

    async def _post_otp(self, request: Request) -> Response:
        if not self._auth or not self._challenge:
            return self._form("Сначала введите логин и пароль.")
        form = await request.form()
        code = str(form.get("code", "")).strip()
        try:
            result = await self._auth.complete_2fa(self._challenge, code)
        except Exception:
            logger.exception("otp failed")
            return self._otp("МИРЭА не ответила. Попробуйте ещё раз.")
        if result.challenge:
            self._challenge = result.challenge
            return self._otp(result.message or "Неверный код.")
        if not result.success or not result.cookies:
            self._challenge = None
            return self._form(result.message or "Подтверждение входа завершилось ошибкой. Войдите заново.", self._login)
        return self._finish(result.cookies)


def build_local(store: SessionStore | None = None) -> tuple[MCPServer, LocalLogin]:
    store = store or SessionStore()
    store.configure()
    mcp = MCPServer(name="mirea-connect", title="MIREA Connect", instructions=INSTRUCTIONS)
    source = LocalSource(store)
    tool = register_tools(mcp, source)
    page = LocalLogin(store)

    @tool(LOCAL_ACTION)
    async def login(open_browser: bool = True) -> dict:
        """Открыть страницу входа в МИРЭА на этом компьютере (127.0.0.1). Пароль вводится в браузере,
        не в чате. Верни пользователю ссылку из ответа; после входа личные инструменты заработают."""
        url = await page.start()
        opened = False
        if open_browser:
            with contextlib.suppress(Exception):
                opened = _open_browser(url)
        return {
            "url": url,
            "browser_opened": opened,
            "message": "Откройте ссылку и войдите в МИРЭА. Страница работает 15 минут и только на этом компьютере.",
        }

    return mcp, page


def run_stdio(store: SessionStore | None = None) -> None:
    setup_logging(logging.WARNING)  # stderr без секретов; stdout занят протоколом MCP
    mcp, _ = build_local(store)
    mcp.run("stdio")
