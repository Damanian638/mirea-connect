"""Удалённый режим: HTTP-сервер с OAuth, страницей входа и инструментами."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pymirea import Config, configure

from .config import Settings
from .db import Database
from .oauth import SCOPE, MireaOAuthProvider, is_guest
from .sessions import SessionVault
from .tools import BASE_INSTRUCTIONS, register_tools
from .web import LoginFlow

logger = logging.getLogger(__name__)

INSTRUCTIONS = BASE_INSTRUCTIONS + (
    " Личные инструменты (my_*) требуют входа в МИРЭА при подключении коннектора; если сессии нет "
    "или она истекла, попроси пользователя отключить и заново подключить коннектор в настройках клиента."
)

RELOGIN_HINT = (
    "Отключите и заново подключите коннектор MIREA в настройках клиента и войдите в МИРЭА — "
    "пароль в чат вводить не нужно."
)


class RemoteSource:
    relogin_hint = RELOGIN_HINT

    def __init__(self, vault: SessionVault, db: Database) -> None:
        self.vault = vault
        self.db = db

    @staticmethod
    def _user() -> str | None:
        token = get_access_token()
        return token.subject if token else None

    @asynccontextmanager
    async def use(self) -> AsyncIterator[dict]:
        user_id = self._user()
        if is_guest(user_id):
            raise ToolError("Вы подключились без входа в МИРЭА — доступно только публичное расписание. " + RELOGIN_HINT)
        async with self.vault.use(user_id) as cookies:
            yield cookies

    def whoami(self) -> dict:
        user_id = self._user()
        if is_guest(user_id):
            return {"mode": "guest", "personal_tools": False}
        info = self.vault.info(user_id) or {}
        return {"mode": "mirea", "login": info.get("login"), "personal_tools": bool(info.get("session_blob"))}

    def forget(self) -> None:
        user_id = self._user()
        if user_id and not is_guest(user_id):
            self.vault.forget(user_id)
        elif user_id:
            self.db.delete_user_tokens(user_id)

    def mark_dead(self) -> None:
        user_id = self._user()
        if user_id and not is_guest(user_id):
            self.db.update_session(user_id, None)


def build(settings: Settings) -> tuple[MCPServer, Database]:
    configure(Config(session_keys=settings.resolve_session_keys(), mirea_proxy=settings.mirea_proxy))
    db = Database(settings.data_dir / "mirea-connect.sqlite3")
    provider = MireaOAuthProvider(db, settings)
    vault = SessionVault(db, settings.session_keys)
    flow = LoginFlow(provider, vault, settings)

    mcp = MCPServer(
        name="mirea-connect",
        title="MIREA Connect",
        instructions=INSTRUCTIONS,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.public_url,
            resource_server_url=settings.mcp_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
            # Токены выдаём сами и только для этого сервера; часть клиентов не передаёт resource.
            validate_token_resource=False,
        ),
    )

    mcp.custom_route("/", methods=["GET"])(flow.index)
    mcp.custom_route("/health", methods=["GET"])(flow.health)
    mcp.custom_route("/login", methods=["GET"])(flow.get_login)
    mcp.custom_route("/login", methods=["POST"])(flow.post_login)
    mcp.custom_route("/login/otp", methods=["POST"])(flow.post_otp)

    register_tools(mcp, RemoteSource(vault, db))
    return mcp, db


def make_app(settings: Settings):
    mcp, _ = build(settings)
    allowed = sorted(
        {settings.public_host, "localhost", "127.0.0.1", f"localhost:{settings.port}", f"127.0.0.1:{settings.port}"}
        | set(settings.extra_allowed_hosts)
    )
    return mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed,
            allowed_origins=[settings.public_url, "https://claude.ai", "https://claude.com"],
        ),
        host=settings.host,
    )


def serve(host: str | None = None, port: int | None = None, public_url: str | None = None) -> None:
    import uvicorn

    settings = Settings.from_env()
    if host:
        settings.host = host
    if port:
        settings.port = port
    if public_url:
        settings.public_url = public_url.rstrip("/")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на INFO пишет полные URL запросов к sso.mirea.ru — там бывают одноразовые коды.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger.info("MIREA Connect: %s (слушаю %s:%s)", settings.mcp_url, settings.host, settings.port)
    uvicorn.run(make_app(settings), host=settings.host, port=settings.port, proxy_headers=True, forwarded_allow_ips="*")
