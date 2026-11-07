"""Зашифрованные сессии Пульса для каждого пользователя."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator

from pymirea.crypto import SessionCrypto
from pymirea.exceptions import MireaSessionExpired
from pymirea.tokens import ensure_fresh_token

from .db import Database


class NoSession(RuntimeError):
    """У пользователя нет живой сессии МИРЭА."""


class SessionVault:
    def __init__(self, db: Database, session_keys: str) -> None:
        self.db = db
        self.crypto = SessionCrypto(session_keys)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def store(self, user_id: str, login: str, cookies: dict) -> None:
        self.db.save_user(user_id, login=login, session_blob=self.crypto.encrypt_session(cookies))

    def info(self, user_id: str) -> dict | None:
        return self.db.get_user(user_id)

    def forget(self, user_id: str) -> None:
        self.db.delete_user(user_id)

    @asynccontextmanager
    async def use(self, user_id: str) -> AsyncIterator[dict]:
        """Выдать cookies пользователя; изменения (обновлённые токены и кеши) сохраняются обратно.

        Вызовы одного пользователя сериализуются — сервисы pymirea мутируют dict
        на месте, а параллельный refresh одного refresh_token сломал бы сессию."""
        async with self._locks[user_id]:
            row = self.db.get_user(user_id)
            if not row or not row.get("session_blob"):
                raise NoSession("Сессия МИРЭА не найдена.")
            cookies = self.crypto.decrypt_session(row["session_blob"])
            if not cookies:
                raise NoSession("Сессию МИРЭА не удалось расшифровать.")
            before = json.dumps(cookies, sort_keys=True, default=str)
            try:
                await ensure_fresh_token(cookies, buffer_s=60)
            except MireaSessionExpired as e:
                self.db.update_session(user_id, None)
                raise NoSession("Сессия МИРЭА истекла.") from e
            try:
                yield cookies
            finally:
                if json.dumps(cookies, sort_keys=True, default=str) != before:
                    self.db.update_session(user_id, self.crypto.encrypt_session(cookies))
