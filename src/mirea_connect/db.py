"""SQLite-хранилище: OAuth-клиенты, коды, токены, сессии пользователей.

Токены и коды хранятся только в виде SHA-256, сессии МИРЭА — зашифрованы
``SessionCrypto`` (Fernet). Пароли не хранятся нигде.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    req_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    params TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    code_hash TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    client_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    grant_id TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tokens_grant ON tokens(grant_id);
CREATE INDEX IF NOT EXISTS tokens_user ON tokens(user_id);
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    login TEXT,
    session_blob TEXT,
    updated_at REAL NOT NULL
);
"""


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def cleanup(self) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM pending WHERE expires_at < ?", (now,))
            self._conn.execute("DELETE FROM codes WHERE expires_at < ?", (now,))
            self._conn.execute("DELETE FROM tokens WHERE expires_at < ?", (now,))

    # --- clients ---

    def save_client(self, client_id: str, data: str) -> None:
        self._exec(
            "INSERT OR REPLACE INTO clients(client_id, data, created_at) VALUES (?, ?, ?)",
            (client_id, data, time.time()),
        )

    def get_client(self, client_id: str) -> str | None:
        row = self._one("SELECT data FROM clients WHERE client_id = ?", (client_id,))
        return row["data"] if row else None

    # --- pending authorizations ---

    def save_pending(self, req_id: str, client_id: str, params: dict, ttl_s: float) -> None:
        self._exec(
            "INSERT INTO pending(req_id, client_id, params, expires_at) VALUES (?, ?, ?, ?)",
            (req_id, client_id, json.dumps(params), time.time() + ttl_s),
        )

    def get_pending(self, req_id: str) -> tuple[str, dict] | None:
        row = self._one(
            "SELECT client_id, params FROM pending WHERE req_id = ? AND expires_at >= ?", (req_id, time.time())
        )
        return (row["client_id"], json.loads(row["params"])) if row else None

    def delete_pending(self, req_id: str) -> None:
        self._exec("DELETE FROM pending WHERE req_id = ?", (req_id,))

    # --- authorization codes ---

    def save_code(self, code: str, data: dict, expires_at: float) -> None:
        self._exec(
            "INSERT INTO codes(code_hash, data, expires_at) VALUES (?, ?, ?)",
            (hash_secret(code), json.dumps(data), expires_at),
        )

    def get_code(self, code: str) -> dict | None:
        row = self._one(
            "SELECT data FROM codes WHERE code_hash = ? AND expires_at >= ?", (hash_secret(code), time.time())
        )
        return json.loads(row["data"]) if row else None

    def delete_code(self, code: str) -> None:
        self._exec("DELETE FROM codes WHERE code_hash = ?", (hash_secret(code),))

    # --- tokens ---

    def save_token(
        self,
        token: str,
        *,
        kind: str,
        client_id: str,
        user_id: str,
        scopes: list[str],
        resource: str | None,
        grant_id: str,
        expires_at: float,
    ) -> None:
        self._exec(
            "INSERT INTO tokens(token_hash, kind, client_id, user_id, scopes, resource, grant_id, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (hash_secret(token), kind, client_id, user_id, json.dumps(scopes), resource, grant_id, expires_at),
        )

    def get_token(self, token: str, kind: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM tokens WHERE token_hash = ? AND kind = ? AND expires_at >= ?",
            (hash_secret(token), kind, time.time()),
        )
        if not row:
            return None
        out = dict(row)
        out["scopes"] = json.loads(out["scopes"])
        return out

    def delete_grant(self, grant_id: str) -> None:
        self._exec("DELETE FROM tokens WHERE grant_id = ?", (grant_id,))

    def delete_user_tokens(self, user_id: str) -> None:
        self._exec("DELETE FROM tokens WHERE user_id = ?", (user_id,))

    # --- users ---

    def save_user(self, user_id: str, *, login: str | None, session_blob: str | None) -> None:
        self._exec(
            "INSERT INTO users(user_id, login, session_blob, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET login = excluded.login,"
            " session_blob = excluded.session_blob, updated_at = excluded.updated_at",
            (user_id, login, session_blob, time.time()),
        )

    def update_session(self, user_id: str, session_blob: str | None) -> None:
        self._exec(
            "UPDATE users SET session_blob = ?, updated_at = ? WHERE user_id = ?",
            (session_blob, time.time(), user_id),
        )

    def get_user(self, user_id: str) -> dict | None:
        row = self._one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return dict(row) if row else None

    def delete_user(self, user_id: str) -> None:
        self._exec("DELETE FROM users WHERE user_id = ?", (user_id,))
        self.delete_user_tokens(user_id)
