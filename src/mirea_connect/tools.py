"""Инструменты MCP, общие для удалённого и локального режимов.

Отличается только источник сессии (:class:`Source`): в удалённом режиме это
зашифрованная сессия пользователя из токена OAuth, в локальном — файл
на устройстве самого пользователя.
"""

from __future__ import annotations

import functools
from contextlib import AbstractAsyncContextManager
from typing import Any, Awaitable, Callable, Protocol

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import services as S
from .sessions import NoSession

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
LOCAL_ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

BASE_INSTRUCTIONS = (
    "Коннектор к РТУ МИРЭА: Пульс (личное расписание, баллы БРС, посещаемость, турникеты) "
    "и публичное расписание групп, преподавателей и аудиторий. Время московское (UTC+3). "
    "Никогда не спрашивай у пользователя пароль или код МИРЭА в чате. "
    "Если пользователь не вошёл, расписание его группы можно получить через timetable по названию группы."
)


class Source(Protocol):
    relogin_hint: str

    def use(self) -> AbstractAsyncContextManager[dict]:
        """Cookies текущего пользователя; изменения сохраняются при выходе."""

    def whoami(self) -> dict: ...

    def forget(self) -> None: ...

    def mark_dead(self) -> None: ...


def _guard(source: Source, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except NoSession as e:
            raise ToolError(f"{e} {source.relogin_hint}") from e
        except S.SessionExpired as e:
            raise ToolError(f"МИРЭА отклонила сессию ({e}). {source.relogin_hint}") from e
        except S.ServiceError as e:
            raise ToolError(str(e)) from e
        except Exception as e:  # сеть, тайм-ауты, изменившийся ответ API — без трейса и URL в ответе
            raise ToolError(str(S.friendly_error(e))) from e

    return wrapper


def register_tools(mcp: Any, source: Source) -> Callable:
    """Зарегистрировать инструменты; вернуть декоратор ``tool`` для режимо-специфичных добавок."""

    def tool(annotations: ToolAnnotations = READ):
        def deco(fn):
            return mcp.tool(annotations=annotations)(_guard(source, fn))

        return deco

    async def personal(op: Callable[[dict], Awaitable[Any]]) -> Any:
        async with source.use() as cookies:
            try:
                return await op(cookies)
            except S.SessionExpired:
                source.mark_dead()
                raise

    @tool()
    async def whoami() -> dict:
        """Кто подключён: логин МИРЭА или гостевой режим, есть ли живая сессия Пульса."""
        return source.whoami()

    @tool(DESTRUCTIVE)
    async def delete_my_data() -> dict:
        """Удалить сохранённую сессию МИРЭА (и, для удалённого сервера, отозвать токены).
        Вызывай только по явной просьбе пользователя."""
        source.forget()
        return {"deleted": True}

    @tool()
    async def my_schedule(days: int = 7) -> dict:
        """Личное расписание из Пульса на ближайшие `days` дней (1–31), начиная с сегодня."""
        return await personal(lambda c: S.my_schedule(c, days))

    @tool()
    async def my_grades() -> dict:
        """Баллы БРС за текущий семестр: текущий контроль (из 40), семестровый (из 30), посещения (из 30),
        достижения, дополнительные, итог (из 120). attendance_max_possible — достижимый максимум за посещения."""
        return await personal(S.grades)

    @tool()
    async def my_attendance(discipline: str) -> dict:
        """Посещаемость по дисциплине: сводка и статус каждого занятия.
        `discipline` — discipline_id из my_grades или часть названия предмета."""
        return await personal(lambda c: S.attendance(c, discipline))

    @tool()
    async def my_turnstile_events() -> dict:
        """Проходы через турникеты (СКУД) за сегодня: время, зоны, длительность."""
        return await personal(S.acs_today)

    @tool()
    async def my_sdo(deadline_limit: int = 50) -> dict:
        """СДО МИРЭА (online-edu.mirea.ru, Moodle): список курсов, ближайшие дедлайны заданий и тестов,
        итоговые оценки по курсам. Это отдельная от Пульса система (курсы, лекции, сдачи), но вход общий."""
        return await personal(lambda c: S.sdo_overview(c, deadline_limit=deadline_limit))

    @tool()
    async def search_timetable(kind: str, query: str) -> list[dict]:
        """Поиск в публичном расписании. kind: group | teacher | classroom. Возвращает совпадения с uid."""
        return await S.search(kind, query)

    @tool()
    async def timetable(kind: str, query: str, days: int = 7, date_from: str | None = None) -> dict:
        """Публичное расписание группы, преподавателя или аудитории (вход не нужен).
        kind: group | teacher | classroom; query — название («ЭФБО-01-24», «Иванов И.И.», «А-101 (В-78)»).
        days — сколько дней показать, начиная с date_from (YYYY-MM-DD, по умолчанию сегодня по Москве); 0 — всё.
        Если под запрос подходят несколько человек или аудиторий, вернётся ошибка со списком вариантов:
        уточни у пользователя, кого он имел в виду, и повтори запрос с полным названием."""
        return await S.timetable(kind, query, days, date_from)

    @tool()
    async def free_rooms(
        start_time: str,
        end_time: str,
        date: str | None = None,
        campus: str | None = None,
        rooms: list[str] | None = None,
    ) -> dict:
        """Свободные аудитории в интервал времени (по расписанию МИРЭА).
        start_time/end_time — HH:MM; date — YYYY-MM-DD, по умолчанию сегодня; campus — корпус («В-78», «С-20», «МП-1»).
        Без rooms вернёт список свободных аудиторий (с фильтром по корпусу); с rooms («А-9», «А-9 (В-78)») —
        для каждой, свободна ли она. Если по корпусу ничего не нашлось, available_campuses — корпуса,
        в которых есть свободные аудитории: проверь, нет ли опечатки в названии корпуса."""
        return await S.free_rooms(rooms, date, start_time, end_time, campus)

    return tool
