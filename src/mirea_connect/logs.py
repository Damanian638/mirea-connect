"""Журналирование без секретов.

pymirea пишет на INFO полные адреса SSO (``session_code``, ``tab_id``, одноразовый
``code`` OAuth), uvicorn — строки запросов. Значения всех параметров запроса
в адресах заменяются на «…»: имена параметров и путь остаются для диагностики.
Вывод — только в stderr: в локальном режиме stdout занят протоколом MCP.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import TextIO

# «?name=value» и «&name=value» — в URL, в пути из access-лога, в тексте исключений.
_QUERY_VALUE = re.compile(r"([?&][\w.\-\[\]]+=)[^\s&#'\"<>]*")


def redact(text: str) -> str:
    return _QUERY_VALUE.sub(r"\1…", text)


class RedactingFormatter(logging.Formatter):
    """Применяется ко всей записи, включая текст исключения и трейс."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: int, *, stream: TextIO | None = None, fmt: str | None = None) -> None:
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(RedactingFormatter(fmt or "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx на INFO пишет каждый запрос к sso.mirea.ru; хватает предупреждений.
    logging.getLogger("httpx").setLevel(logging.WARNING)
