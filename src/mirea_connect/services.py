"""Обращения к МИРЭА через pymirea. Возвращают JSON-сериализуемые структуры."""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from pymirea.acs import MireaACS
from pymirea.schedule import MireaSchedule, Schedule
from pymirea.sdo import MireaSDO

from .pulse import MireaGrades

MSK = timezone(timedelta(hours=3))
ENTITY_KINDS = ("group", "teacher", "classroom")
_ATTEND_TYPES = {0: "unknown", 1: "absent", 2: "excused", 3: "present"}

_SESSION_EXPIRED_HINTS = ("сессия истек", "перелог", "needs_auth", "401", "cookie")


class ServiceError(RuntimeError):
    """Ошибка, которую стоит показать пользователю как есть."""


class SessionExpired(ServiceError):
    """МИРЭА отвергла сессию."""


def _fail(message: str) -> ServiceError:
    low = (message or "").lower()
    if any(h in low for h in _SESSION_EXPIRED_HINTS):
        return SessionExpired(message)
    return ServiceError(message or "МИРЭА вернула ошибку")


def _iso(epoch: float | None) -> str | None:
    return datetime.fromtimestamp(epoch, MSK).isoformat(timespec="minutes") if epoch else None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=MSK)


# --- личное ---


async def my_schedule(cookies: dict, days: int) -> dict:
    days = max(1, min(int(days), 31))
    api = MireaGrades(session_cookies=cookies)
    try:
        res = await api.get_schedule(days=days)
    finally:
        await api.close()
    if not res.success and res.message != "Нет пар в ближайшие дни":
        raise _fail(res.message)
    return {
        "days": days,
        "lessons": [
            {
                "start": _iso(l.start_epoch),
                "end": _iso(l.end_epoch),
                "name": l.name,
                "type": l.lesson_type or None,
                "room": l.room or None,
                "teacher": l.teacher or None,
                "subgroup": l.subgroup or None,
            }
            for l in (res.lessons or [])
        ],
    }


async def grades(cookies: dict) -> dict:
    api = MireaGrades(session_cookies=cookies)
    try:
        res = await api.get_grades()
    finally:
        await api.close()
    if not res.success:
        raise _fail(res.message)
    return {"subjects": [asdict(s) for s in (res.subjects or [])]}


async def attendance(cookies: dict, discipline: str) -> dict:
    query = (discipline or "").strip()
    if not query:
        raise ServiceError("Укажите дисциплину (discipline_id или часть названия).")
    api = MireaGrades(session_cookies=cookies)
    try:
        gr = await api.get_grades()
        if not gr.success:
            raise _fail(gr.message)
        subjects = gr.subjects or []
        match = [s for s in subjects if s.discipline_id == query]
        if not match:
            q = query.casefold()
            match = [s for s in subjects if q in (s.name or "").casefold()]
        if not match:
            raise ServiceError(f"Дисциплина «{query}» не найдена среди {len(subjects)} предметов.")
        if len(match) > 1:
            raise ServiceError(f"Под «{query}» подходит несколько дисциплин: {'; '.join(s.name for s in match)}.")
        subj = match[0]
        if not subj.discipline_id:
            raise ServiceError(f"У дисциплины «{subj.name}» нет discipline_id.")
        res = await api.get_attendance_detail(subj.discipline_id, gr.semester)
    finally:
        await api.close()
    if not res.success:
        raise _fail(res.message)
    return {
        "discipline": subj.name,
        "discipline_id": subj.discipline_id,
        "summary": asdict(res.summary) if res.summary else None,
        "entries": [
            {"lesson_start": _iso(e.lesson_start), "status": _ATTEND_TYPES.get(e.attend_type or 0, "unknown")}
            for e in (res.entries or [])
        ],
    }


async def acs_today(cookies: dict) -> dict:
    api = MireaACS(session_cookies=cookies)
    try:
        res = await api.get_today_events()
    finally:
        await api.close()
    if not res.success:
        raise _fail(res.message)
    return {"date": res.date, "events": [asdict(e) for e in res.events]}


async def sdo_overview(cookies: dict, *, deadline_limit: int = 50) -> dict:
    """СДО (Moodle): курсы, ближайшие дедлайны, оценки — одним вызовом."""
    api = MireaSDO(session_cookies=cookies)
    try:
        courses = await api.get_courses()
        deadlines = await api.get_deadlines(limit=deadline_limit)
        names = {c.id: c.name for c in (courses.courses or [])}
        grades = await api.get_grades(course_names=names)
    finally:
        await api.close()
    if not courses.success and not deadlines.success and not grades.success:
        raise _fail(courses.message or deadlines.message or grades.message)
    return {
        "courses": [asdict(c) for c in (courses.courses or [])],
        "deadlines": [
            {
                "name": d.name,
                "course": d.course,
                "when": _iso(d.timestamp),
                "activity": d.activity or None,
                "url": d.url or None,
            }
            for d in (deadlines.deadlines or [])
        ],
        "grades": [asdict(g) for g in (grades.grades or []) if g.grade],
    }


# --- публичное ---


async def _search(sched: MireaSchedule, kind: str, query: str):
    if kind == "group":
        return await sched.search_groups(query)
    if kind == "teacher":
        return await sched.search_teachers(query)
    if kind == "classroom":
        return await sched.search_classrooms(query)
    raise ServiceError(f"kind должен быть одним из {ENTITY_KINDS}")


async def search(kind: str, query: str) -> list[dict]:
    sched = MireaSchedule()
    try:
        return [asdict(i) for i in await _search(sched, kind, query)]
    finally:
        await sched.close()


async def fetch_entity(kind: str, query: str) -> Schedule:
    sched = MireaSchedule()
    try:
        found = await _search(sched, kind, query)
        if not found:
            raise ServiceError(f"По запросу «{query}» ничего не найдено ({kind}).")
        q = query.casefold().strip()
        exact = [f for f in found if (f.name or "").casefold().strip() == q]
        pick = exact[0] if exact else found[0]
        getter = {
            "group": sched.get_group_schedule,
            "teacher": sched.get_teacher_schedule,
            "classroom": sched.get_classroom_schedule,
        }[kind]
        return await getter(uid=pick.uid or pick.name, name=pick.name)
    finally:
        await sched.close()


def _events(schedule: Schedule, start: datetime | None, days: int | None) -> list[dict]:
    events = schedule.events
    if start is not None and days:
        end = start + timedelta(days=days)
        events = [e for e in events if start <= _aware(e.start) < end]
    return [
        {
            "start": _aware(e.start).isoformat(timespec="minutes"),
            "end": _aware(e.end).isoformat(timespec="minutes") if e.end else None,
            "summary": e.summary,
            "location": e.location,
            "description": e.description or None,
        }
        for e in events
    ]


def _start_of(date_from: str | None) -> datetime:
    if date_from:
        try:
            return datetime.fromisoformat(date_from).replace(tzinfo=MSK)
        except ValueError as e:
            raise ServiceError("date_from должен быть в формате YYYY-MM-DD") from e
    return datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)


async def timetable(kind: str, query: str, days: int | None, date_from: str | None) -> dict:
    result = await fetch_entity(kind, query)
    start = _start_of(date_from)
    days = max(0, min(int(days or 0), 180)) or None
    return {
        "kind": kind,
        "name": result.entity_name,
        "uid": result.entity_uid,
        "from": start.date().isoformat() if days else None,
        "days": days,
        "events": _events(result, start, days),
    }


def _split_room(value: str) -> tuple[str, str | None]:
    """«А-9 (В-78)» → («А-9», «В-78»)."""
    m = re.match(r"^\s*(.+?)\s*\(([^()]+)\)\s*$", value or "")
    return (m.group(1), m.group(2).strip()) if m else ((value or "").strip(), None)


def _norm(value: str | None) -> str:
    return (value or "").casefold().replace(" ", "").replace("ё", "е")


async def free_rooms(
    rooms: list[str] | None, date: str | None, start_time: str, end_time: str, campus: str | None = None
) -> dict:
    """Свободные аудитории в интервал: проверить конкретные или перечислить все (с фильтром по корпусу)."""
    if rooms and len(rooms) > 50:
        raise ServiceError("Не больше 50 аудиторий за раз.")
    day = _start_of(date)
    try:
        sh, sm = (int(x) for x in start_time.split(":"))
        eh, em = (int(x) for x in end_time.split(":"))
    except ValueError as e:
        raise ServiceError("Время в формате HH:MM") from e
    t0 = day.replace(hour=sh, minute=sm)
    t1 = day.replace(hour=eh, minute=em)
    if t1 <= t0:
        raise ServiceError("end_time должно быть позже start_time")

    sched = MireaSchedule()
    try:
        free = await sched.get_free_rooms(t0, t1)
    finally:
        await sched.close()
    if free is None:
        raise ServiceError("Сервис свободных аудиторий недоступен, попробуйте позже.")

    head = {"date": day.date().isoformat(), "from": start_time, "to": end_time}
    if rooms:
        index = {(_norm(f["room"]), _norm(f.get("campus"))) for f in free}
        names = {_norm(f["room"]) for f in free}
        out = []
        for raw in rooms:
            room, room_campus = _split_room(raw)
            room_campus = room_campus or campus
            ok = (_norm(room), _norm(room_campus)) in index if room_campus else _norm(room) in names
            out.append({"room": raw, "free": ok})
        return {**head, "rooms": out}

    if campus:
        free = [f for f in free if _norm(f.get("campus")) == _norm(campus)]
    total = len(free)
    return {**head, "campus": campus, "total_free": total, "free_rooms": free[:300], "truncated": total > 300}
