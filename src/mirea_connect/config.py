"""Настройки сервера из переменных окружения."""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    public_url: str = "http://localhost:8000"
    """Внешний адрес сервера (как его видят клиенты), без завершающего слэша."""

    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = field(default_factory=lambda: Path("data"))
    session_keys: str = ""
    """Ключ(и) шифрования сессий МИРЭА. Пусто — сгенерировать и сохранить в data_dir/key."""

    mirea_proxy: str | None = None
    allow_guest: bool = True
    """Разрешить подключение без входа в МИРЭА (только публичное расписание)."""

    access_token_ttl_s: int = 3600
    refresh_token_ttl_s: int = 60 * 60 * 24 * 30
    extra_allowed_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            public_url=(os.environ.get("PUBLIC_URL") or "http://localhost:8000").rstrip("/"),
            host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")),
            data_dir=Path(os.environ.get("DATA_DIR", "data")),
            session_keys=os.environ.get("SESSION_KEYS", ""),
            mirea_proxy=os.environ.get("MIREA_PROXY") or None,
            allow_guest=_env_bool("ALLOW_GUEST", True),
            extra_allowed_hosts=[h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()],
        )
        return s

    @property
    def public_host(self) -> str:
        return urlparse(self.public_url).netloc

    @property
    def mcp_url(self) -> str:
        return f"{self.public_url}/mcp"

    def resolve_session_keys(self) -> str:
        if self.session_keys:
            return self.session_keys
        self.data_dir.mkdir(parents=True, exist_ok=True)
        key_path = self.data_dir / "key"
        if key_path.exists():
            self.session_keys = key_path.read_text(encoding="utf-8").strip()
        else:
            self.session_keys = base64.b64encode(secrets.token_bytes(32)).decode()
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self.session_keys + "\n")
        return self.session_keys
