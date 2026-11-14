"""Обращения к МИРЭА через pymirea. Возвращают JSON-сериализуемые структуры."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pymirea.acs import MireaACS
from pymirea.schedule import MireaSchedule, Schedule

from .pulse import MireaGrades
from .sdo import MireaSDO
from .sdo_course import CourseError, CourseSDO

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))
ENTITY_KINDS = ("group", "teacher", "classroom")
_KIND_LABELS = {"group": "групп", "teacher": "преподавателей", "classroom": "аудиторий"}
_ATTEND_TYPES = {0: "unknown", 1: "absent", 2: "excused", 3: "present"}

_SESSION_EXPIRED_HINTS = ("сессия истек", "перелог", "needs_auth", "401", "cookie")


class ServiceError(RuntimeError):
    """Ошибка, которую стоит показать пользователю как есть."""


class SessionExpired(ServiceError):
    """МИРЭА отвергла сессию."""


def friendly_error(exc: BaseException) -> ServiceError:
    """Понятная ошибка вместо трейса или текста исключения.

    Текст сетевых исключений может содержать адреса SSO с одноразовыми
    параметрами, поэтому пользователю он не показывается."""
    if isinstance(exc, ServiceError):
        return exc
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return ServiceError("МИРЭА не ответила вовремя. Повторите запрос через минуту.")
    if isinstance(exc, (httpx.HTTPError, OSError)):
        return ServiceError("Не удалось связаться с сервисами МИРЭА: нет сети или сервис недоступен. Повторите позже.")
    logger.warning("unexpected %s from MIREA services", type(exc).__name__, exc_info=exc)
    return ServiceError(
        "Неожиданный ответ от сервисов МИРЭА — возможно, изменился их интерфейс. "
        "Повторите позже; если ошибка повторяется, сообщите о ней разработчикам."
    )


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
        # Moodle has its own session. Its failure must not revoke Pulse/SSO
        # credentials through personal()'s SessionExpired handler.
        raise ServiceError(
            "СДО не подтвердила доступ. Сессия Пульса сохранена. "
            + (courses.message or deadlines.message or grades.message or "Не удалось получить данные СДО.")
        )
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


async def sdo_course_operation(cookies: dict, operation: str, **kwargs) -> dict:
    """Shared lifecycle; Moodle failures must never revoke the Pulse session."""
    api = CourseSDO(session_cookies=cookies)
    try:
        return await getattr(api, operation)(**kwargs)
    except CourseError as exc:
        raise ServiceError(str(exc)) from None
    except httpx.HTTPError:
        raise ServiceError(
            "Ошибка связи с СДО. Если операция сохраняла данные, результат неизвестен: "
            "прочитайте структуру курса перед повтором. Запрос автоматически не повторяется."
        ) from None
    finally:
        await api.close()


async def _search(sched: MireaSchedule, kind: str, query: str):
    if kind == "group":
        return await sched.search_groups(query)
    if kind == "teacher":
        return await sched.search_teachers(query)
    if kind == "classroom":
        return await sched.search_classrooms(query)
    raise ServiceError(f"kind должен быть одним из {ENTITY_KINDS}")


def _query(kind: str, query: str | None) -> str:
    if kind not in ENTITY_KINDS:
        raise ServiceError(f"kind должен быть одним из {ENTITY_KINDS}")
    q = " ".join((query or "").split())
    if not q:
        raise ServiceError(
            "Укажите, что искать: название группы («ЭФБО-01-24»), фамилию преподавателя или номер аудитории."
        )
    return q


async def search(kind: str, query: str) -> list[dict]:
    query = _query(kind, query)
    sched = MireaSchedule()
    try:
        return [asdict(i) for i in await _search(sched, kind, query)]
    finally:
        await sched.close()


def _name_tokens(value: str | None) -> list[str]:
    return [t for t in re.split(r"[\s.]+", _norm_letters(value)) if t]


def _teacher_matches(name: str | None, query: str) -> bool:
    """«Иванов И. И.», «Иванов Илья Ив» → «Иванов Илья Иванович»: фамилия целиком, остальное — по началу."""
    cand, q = _name_tokens(name), _name_tokens(query)
    if not q or len(q) > len(cand) or cand[0] != q[0]:
        return False
    return all(c.startswith(t) for c, t in zip(cand[1:], q[1:]))


def _pick(found: list, kind: str, query: str):
    """Выбрать одну сущность; при неоднозначности — ошибка со списком вариантов, а не первый попавшийся."""
    key = _name_tokens(query)
    exact = [f for f in found if _name_tokens(f.name) == key]
    if len(exact) == 1 or (exact and kind != "classroom"):
        return exact[0]
    if kind == "teacher" and len(key) > 1:
        close = [f for f in found if _teacher_matches(f.name, query)]
        if len(close) == 1:
            return close[0]
        found = close or found
    if kind == "classroom":
        room, campus = _split_room(query)
        same_room = [f for f in found if _norm(_split_room(f.name)[0]) == _norm(room)]
        if campus:
            same_room = [f for f in same_room if _norm(_split_room(f.name)[1]) == _norm(campus)]
        if len(same_room) == 1:
            return same_room[0]
        found = same_room or found
    if len(found) == 1:
        return found[0]
    names = [f.name for f in found if f.name]
    shown = "; ".join(names[:10]) + (f" и ещё {len(names) - 10}" if len(names) > 10 else "")
    if kind == "teacher":
        hint = "полное ФИО" if len(key) > 1 else "фамилию и инициалы или полное ФИО"
    else:
        hint = "точное название"
    raise ServiceError(
        f"По запросу «{query}» найдено несколько {_KIND_LABELS[kind]}: {shown}. "
        f"Уточните запрос — укажите {hint}."
    )


async def fetch_entity(kind: str, query: str) -> Schedule:
    query = _query(kind, query)
    sched = MireaSchedule()
    try:
        found = [f for f in await _search(sched, kind, query) if f.name]
        if not found:
            raise ServiceError(
                f"По запросу «{query}» ничего не найдено ({kind}). Проверьте написание; "
                "если оно верное, сервис расписания мог быть временно недоступен — повторите позже."
            )
        pick = _pick(found, kind, query)
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


def _start_of(value: str | None, field: str = "date_from") -> datetime:
    """Полночь указанной даты (или сегодня) по Москве — независимо от часового пояса системы."""
    raw = (value or "").strip()
    if raw:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            raise ServiceError(f"{field} должен быть датой в формате ГГГГ-ММ-ДД, например 2026-11-09 (без времени).")
        try:
            day = _date.fromisoformat(raw)
        except ValueError as e:
            raise ServiceError(f"{field}: такой даты нет ({raw}). Формат ГГГГ-ММ-ДД, например 2026-11-09.") from e
        return datetime(day.year, day.month, day.day, tzinfo=MSK)
    return datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_time(value: str | None, field: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (value or "").strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ServiceError(f"{field} должно быть временем в формате ЧЧ:ММ от 00:00 до 23:59, например 10:40.")
    return int(m.group(1)), int(m.group(2))


async def timetable(kind: str, query: str, days: int | None, date_from: str | None) -> dict:
    if days is not None and int(days) < 0:
        raise ServiceError("days не может быть отрицательным: укажите число дней (1–180) или 0 — всё расписание.")
    start = _start_of(date_from)
    result = await fetch_entity(kind, query)
    days = min(int(days or 0), 180) or None
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


# Латинские буквы, неотличимые от кириллических: «B-78» с английской раскладки = «В-78».
_LOOKALIKES = str.maketrans("abcehkmoptxy", "авсенкмортху")


def _norm_letters(value: str | None) -> str:
    return (value or "").casefold().replace("ё", "е").translate(_LOOKALIKES)


def _norm(value: str | None) -> str:
    return _norm_letters(value).replace(" ", "")


async def free_rooms(
    rooms: list[str] | None, date: str | None, start_time: str, end_time: str, campus: str | None = None
) -> dict:
    """Свободные аудитории в интервал: проверить конкретные или перечислить все (с фильтром по корпусу)."""
    if rooms and len(rooms) > 50:
        raise ServiceError("Не больше 50 аудиторий за раз.")
    day = _start_of(date, "date")
    sh, sm = _parse_time(start_time, "start_time")
    eh, em = _parse_time(end_time, "end_time")
    start_time, end_time = f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"
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

    out = {}
    if campus:
        campuses = sorted({f.get("campus") for f in free if f.get("campus")})
        free = [f for f in free if _norm(f.get("campus")) == _norm(campus)]
        if not free:
            # Пустой результат из-за опечатки в корпусе не должен выглядеть как «всё занято».
            out["available_campuses"] = campuses
    total = len(free)
    return {**head, "campus": campus, "total_free": total, "free_rooms": free[:300], "truncated": total > 300, **out}


BELL_SLOTS = (
    (1, "09:00", "10:30"),
    (2, "10:40", "12:10"),
    (3, "12:40", "14:10"),
    (4, "14:20", "15:50"),
    (5, "16:20", "17:50"),
    (6, "18:00", "19:30"),
    (7, "19:40", "21:10"),
    (8, "21:20", "22:50"),
)


def _bell_pair(start_hm: str) -> int | None:
    for num, s_start, _ in BELL_SLOTS:
        if s_start == start_hm:
            return num
    return None


async def teacher_lessons(cookies: dict, date: str | None = None) -> dict:
    day = _start_of(date, "date")
    api = MireaGrades(session_cookies=cookies)
    try:
        raw_lessons, err = await api.get_my_lessons_for_date(day.year, day.month, day.day)
    finally:
        await api.close()
    if err:
        raise _fail(err)
    lessons = sorted(raw_lessons or [], key=lambda l: l.get("start_epoch") or 0)
    items = []
    for idx, l in enumerate(lessons):
        st = l.get("start_epoch") or 0
        et = l.get("end_epoch") or 0
        start_dt = datetime.fromtimestamp(st, MSK) if st else None
        end_dt = datetime.fromtimestamp(et, MSK) if et else None
        shm = start_dt.strftime("%H:%M") if start_dt else ""
        ehm = end_dt.strftime("%H:%M") if end_dt else ""
        time_str = f"{shm} - {ehm}" if shm and ehm else ""
        bell = _bell_pair(shm)
        groups = [v["title"] for v in l.get("visiting_logs", []) if v.get("title")]
        v_ids = [v["id"] for v in l.get("visiting_logs", []) if v.get("id")]
        items.append({
            "order": idx + 1,
            "pair_number": bell or (idx + 1),
            "time": time_str,
            "discipline": l.get("discipline") or "",
            "lesson_type": l.get("lesson_type") or "",
            "auditoriums": l.get("auditoriums") or [],
            "groups": groups,
            "lesson_id": l.get("id") or "",
            "visiting_log_ids": v_ids,
        })
    return {"date": day.date().isoformat(), "count": len(items), "lessons": items}


async def teacher_lesson_students(cookies: dict, lesson_id: str, visiting_log_id: str | None = None) -> dict:
    lid = (lesson_id or "").strip()
    if not lid:
        raise ServiceError("Укажите lesson_id.")

    api = MireaGrades(session_cookies=cookies)
    try:
        vlogs = []
        if visiting_log_id:
            vlogs = [{"id": visiting_log_id.strip(), "title": ""}]
        else:
            now = datetime.now(MSK)
            found_lesson = None
            for day_offset in (0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6, -7, 7):
                d = now + timedelta(days=day_offset)
                day_lessons, _ = await api.get_my_lessons_for_date(d.year, d.month, d.day)
                if day_lessons:
                    for l in day_lessons:
                        if l.get("id") == lid:
                            found_lesson = l
                            break
                if found_lesson:
                    break
            if found_lesson and found_lesson.get("visiting_logs"):
                vlogs = found_lesson["visiting_logs"]

        if not vlogs:
            raise ServiceError(f"Для занятия {lid} не удалось определить visiting_log_id. Укажите его явно.")

        all_students = []
        seen_ids = set()
        for v in vlogs:
            v_id = v["id"]
            group_title = v.get("title") or ""
            students, err = await api.get_attendance_for_lesson(lid, v_id)
            if err:
                raise _fail(err)
            for s in (students or []):
                sid = s.get("student_id")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    all_students.append({
                        "student_id": sid,
                        "fullname": s.get("fullname") or "",
                        "status": s.get("status") or "none",
                        "group": group_title,
                        "attendance_id": s.get("attendance_id") or "",
                    })
    finally:
        await api.close()

    return {
        "lesson_id": lid,
        "total_students": len(all_students),
        "students": all_students,
    }


def _match_student_by_name(name_query: str, students: list[dict]) -> dict:
    query = (name_query or "").strip().casefold()
    if not query:
        raise ServiceError("Пустое имя студента в отметках.")

    q_parts = [p for p in re.split(r"[\s,\.-]+", query) if p]
    exact = []
    prefix = []

    for s in students:
        fn = (s.get("fullname") or "").casefold()
        fn_parts = [p for p in re.split(r"[\s,\.-]+", fn) if p]
        if fn == query:
            exact.append(s)
            continue
        if all(any(sp.startswith(qp) for sp in fn_parts) for qp in q_parts):
            prefix.append(s)

    candidates = exact if exact else prefix
    if not candidates:
        raise ServiceError(f"Студент «{name_query}» не найден среди студентов занятия.")
    if len(candidates) > 1:
        names = "; ".join(c.get("fullname", "") for c in candidates)
        raise ServiceError(f"Под «{name_query}» подходит несколько студентов: {names}. Уточните ФИО.")
    return candidates[0]


def _resolve_attend_type(val: Any) -> int:
    s = str(val or "").strip().lower()
    if s in ("present", "+", "был", "присутствовал", "присутствие", "3", "true"):
        return 3
    if s in ("absent", "н", "n", "отсутствовал", "отсутствие", "1", "false"):
        return 1
    if s in ("clear", "none", "0", "сброс", "удалить", ""):
        return 0
    raise ServiceError(f"Неизвестный статус отметки «{val}». Ожидается: 'present' (+), 'absent' (Н) или 'clear'.")


async def teacher_mark_attendance(
    cookies: dict, lesson_id: str, marks: list[dict], visiting_log_id: str | None = None
) -> dict:
    lid = (lesson_id or "").strip()
    if not lid:
        raise ServiceError("Укажите lesson_id.")
    if not marks:
        raise ServiceError("Список отметок marks пуст.")

    roster_data = await teacher_lesson_students(cookies, lid, visiting_log_id)
    students_roster = roster_data.get("students", [])
    roster_by_sid = {s["student_id"]: s for s in students_roster if s.get("student_id")}

    resolved_records = []
    marks_summary = []
    for m in marks:
        sid = m.get("student_id")
        student_name = m.get("name") or m.get("student")
        att_id = m.get("attendance_id")
        if not sid:
            matched = _match_student_by_name(str(student_name or ""), students_roster)
            sid = matched["student_id"]
            student_name = matched["fullname"]
            att_id = matched.get("attendance_id")
        elif not att_id and sid in roster_by_sid:
            att_id = roster_by_sid[sid].get("attendance_id")
            if not student_name:
                student_name = roster_by_sid[sid].get("fullname")

        attend_type = _resolve_attend_type(m.get("status"))
        status_label = "present" if attend_type == 3 else ("absent" if attend_type == 1 else "clear")
        record = {"student_id": sid, "attend_type": attend_type}
        if att_id:
            record["attendance_id"] = att_id
        resolved_records.append(record)
        marks_summary.append({
            "student": student_name or sid,
            "student_id": sid,
            "status": status_label,
            "attend_type": attend_type,
        })

    api = MireaGrades(session_cookies=cookies)
    try:
        ok, err = await api.update_attendance(lid, resolved_records)
    finally:
        await api.close()
    if not ok:
        raise _fail(err or "Не удалось обновить посещаемость")

    return {
        "success": True,
        "lesson_id": lid,
        "updated_count": len(resolved_records),
        "marks": marks_summary,
    }
