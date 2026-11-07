"""OAuth 2.1 authorization server для MCP-коннектора.

Поток: клиент (claude.ai, Claude Code, Cursor…) регистрируется через DCR,
открывает ``/authorize`` → мы редиректим на свою страницу ``/login``, где
человек входит в МИРЭА (логин, пароль, OTP) или выбирает гостевой режим →
выдаём authorization code → клиент меняет его на access/refresh токены.

Субъект токена (``subject``) — ``user_id``: ``u_<sha256(логин)>`` для
вошедших или ``guest_<random>`` для гостей.
"""

from __future__ import annotations

import hashlib
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import Settings
from .db import Database

PENDING_TTL_S = 15 * 60
CODE_TTL_S = 5 * 60
SCOPE = "mirea"


def user_id_for_login(login: str) -> str:
    return "u_" + hashlib.sha256(login.strip().lower().encode()).hexdigest()[:24]


def new_guest_id() -> str:
    return "guest_" + secrets.token_hex(12)


def is_guest(user_id: str | None) -> bool:
    return not user_id or user_id.startswith("guest_")


class MireaOAuthProvider:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # --- clients (DCR) ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self.db.get_client(client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.db.save_client(client_info.client_id, client_info.model_dump_json())

    # --- authorize ---

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.scopes and any(s != SCOPE for s in params.scopes):
            raise AuthorizeError("invalid_scope", f"Поддерживается только scope «{SCOPE}»")
        self.db.cleanup()
        req_id = secrets.token_urlsafe(24)
        self.db.save_pending(req_id, client.client_id, params.model_dump(mode="json"), PENDING_TTL_S)
        return f"{self.settings.public_url}/login?req={req_id}"

    def describe_pending(self, req_id: str) -> dict | None:
        """Данные о незавершённой авторизации для страницы входа."""
        pending = self.db.get_pending(req_id)
        if not pending:
            return None
        client_id, params = pending
        raw = self.db.get_client(client_id)
        client_name = None
        if raw:
            client_name = OAuthClientInformationFull.model_validate_json(raw).client_name
        return {"client_id": client_id, "client_name": client_name, "params": params}

    def complete_authorization(self, req_id: str, user_id: str) -> str:
        """Выдать authorization code и вернуть URL редиректа обратно в клиент."""
        pending = self.db.get_pending(req_id)
        if not pending:
            raise AuthorizeError("invalid_request", "Запрос авторизации устарел, начните подключение заново")
        client_id, params = pending
        self.db.delete_pending(req_id)
        code = secrets.token_urlsafe(32)
        expires_at = time.time() + CODE_TTL_S
        self.db.save_code(
            code,
            {
                "client_id": client_id,
                "scopes": params.get("scopes") or [SCOPE],
                "code_challenge": params["code_challenge"],
                "redirect_uri": params["redirect_uri"],
                "redirect_uri_provided_explicitly": params["redirect_uri_provided_explicitly"],
                "resource": params.get("resource"),
                "subject": user_id,
                "expires_at": expires_at,
            },
            expires_at,
        )
        return construct_redirect_uri(str(params["redirect_uri"]), code=code, state=params.get("state"))

    def deny_authorization(self, req_id: str) -> str | None:
        pending = self.db.get_pending(req_id)
        if not pending:
            return None
        _, params = pending
        self.db.delete_pending(req_id)
        return construct_redirect_uri(
            str(params["redirect_uri"]), error="access_denied", state=params.get("state")
        )

    # --- code exchange ---

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self.db.get_code(authorization_code)
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **data)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if not self.db.get_code(authorization_code.code):
            raise TokenError("invalid_grant", "Код уже использован или истёк")
        self.db.delete_code(authorization_code.code)
        return self._issue(
            client.client_id,
            authorization_code.subject or new_guest_id(),
            authorization_code.scopes,
            authorization_code.resource,
        )

    # --- refresh ---

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self.db.get_token(refresh_token, "refresh")
        if not row or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=row["user_id"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        row = self.db.get_token(refresh_token.token, "refresh")
        if not row:
            raise TokenError("invalid_grant", "Refresh token недействителен")
        self.db.delete_grant(row["grant_id"])  # ротация: старая пара больше не работает
        return self._issue(client.client_id, row["user_id"], scopes or row["scopes"], row["resource"])

    # --- access ---

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.db.get_token(token, "access")
        if not row:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires_at"]),
            resource=row["resource"],
            subject=row["user_id"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "refresh" if isinstance(token, RefreshToken) else "access"
        row = self.db.get_token(token.token, kind)
        if row:
            self.db.delete_grant(row["grant_id"])

    # --- helpers ---

    def _issue(self, client_id: str, user_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        now = time.time()
        grant_id = secrets.token_hex(16)
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        common = dict(client_id=client_id, user_id=user_id, scopes=scopes, resource=resource, grant_id=grant_id)
        self.db.save_token(access, kind="access", expires_at=now + self.settings.access_token_ttl_s, **common)
        self.db.save_token(refresh, kind="refresh", expires_at=now + self.settings.refresh_token_ttl_s, **common)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.settings.access_token_ttl_s,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )
