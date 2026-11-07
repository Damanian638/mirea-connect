"""CLI-команды ``mirea-connect``: те же данные, что у MCP-инструментов, в терминале.

Личные команды берут ту же локальную сессию, что и MCP в локальном режиме
(``~/.config/pymirea``), так что один ``mirea-connect login`` включает оба.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable

from . import services as S

_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# --- форматирование ---


def _json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _day(iso: str | None) -> str:
    if not iso:
        return "?"
    dt = datetime.fromisoformat(iso)
    return f"{dt.strftime('%d.%m')} ({_DAYS[dt.weekday()]})"


def _hm(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M") if iso else "??:??"


def _num(x: Any) -> str:
    if x is None:
        return "—"
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(f)) if f.is_integer() else f"{f:.1f}"


def _by_day(items: list[dict], start_key: str, line: Callable[[dict], str]) -> None:
    current = None
    for it in items:
        day = _day(it.get(start_key))
        if day != current:
            print(f"\n=== {day} ===")
            current = day
        print("  " + line(it))


# --- рендеры ---


def _render_lessons(data: dict) -> None:
    lessons = data.get("lessons") or []
    if not lessons:
        print("Пар не найдено.")
        return

    def line(lsn: dict) -> str:
        kind = f"[{lsn['type']}] " if lsn.get("type") else ""
        extra = ", ".join(x for x in (lsn.get("room"), lsn.get("teacher"), lsn.get("subgroup")) if x)
        return f"{_hm(lsn['start'])}-{_hm(lsn['end'])}  {kind}{lsn['name']}" + (f"  — {extra}" if extra else "")

    _by_day(lessons, "start", line)


def _render_timetable(data: dict) -> None:
    print(f"{data.get('name') or '?'} ({data.get('kind')})")
    events = data.get("events") or []
    if not events:
        print("Занятий в выбранном диапазоне нет.")
        return

    def line(ev: dict) -> str:
        loc = f"  — {ev['location']}" if ev.get("location") else ""
        return f"{_hm(ev['start'])}-{_hm(ev['end'])}  {ev['summary']}{loc}"

    _by_day(events, "start", line)


def _render_grades(data: dict) -> None:
    subjects = data.get("subjects") or []
    if not subjects:
        print("Предметов нет.")
        return
    width = min(max(len(s["name"]) for s in subjects), 48)
    print(f"{'Предмет':<{width}}  ТК  СК  Пос  Дост  Доп  Итог")
    for s in subjects:
        att = _num(s["attendance"])
        if s.get("attendance_max_possible") is not None:
            att += f"/{_num(s['attendance_max_possible'])}"
        print(
            f"{s['name'][:width]:<{width}}  {_num(s['current_control']):>2}  {_num(s['semester_control']):>2}"
            f"  {att:>4}  {_num(s['achievements']):>4}  {_num(s['additional']):>3}  {_num(s['total']):>4}"
        )


def _render_attendance(data: dict) -> None:
    print(data.get("discipline") or "")
    s = data.get("summary")
    if s:
        print(f"Всего: {s['total_lessons']}  был: {s['present']}  уваж.: {s['excused']}  пропуск: {s['absent']}")
    marks = {"present": "✓", "excused": "~", "absent": "✗", "unknown": "?"}
    for e in data.get("entries") or []:
        print(f"  {marks.get(e['status'], '?')} {e['lesson_start'] or '?'}")


def _render_acs(data: dict) -> None:
    events = data.get("events") or []
    if not events:
        print("Сегодня проходов через турникеты нет.")
        return
    for e in events:
        dur = f"  ({e['duration_label']})" if e.get("duration_label") else ""
        print(f"  {e.get('time_label', '?')}  {e.get('exit_zone') or '?'} → {e.get('enter_zone') or '?'}{dur}")


def _render_sdo(data: dict) -> None:
    courses = data.get("courses") or []
    print(f"Курсы ({len(courses)}):")
    for c in courses:
        print(f"  • {c['name']}")
    deadlines = data.get("deadlines") or []
    print(f"\nДедлайны ({len(deadlines)}):")
    for d in deadlines:
        print(f"  {d.get('when') or '?'}  {d['name']}  — {d.get('course') or ''}")
    grades = [g for g in (data.get("grades") or []) if g.get("grade")]
    if grades:
        print(f"\nОценки ({len(grades)}):")
        for g in grades:
            print(f"  {g['grade']:>8}  {g['course']}")


def _render_search(items: list[dict]) -> None:
    if not items:
        print("Ничего не найдено.")
        return
    for i in items:
        campus = ""
        if isinstance(i.get("campus"), dict):
            campus = f"  [{i['campus'].get('short_name') or i['campus'].get('name')}]"
        print(f"  {i['name']}  (uid={i.get('uid')}){campus}")


def _render_free_rooms(data: dict) -> None:
    rooms = data.get("rooms")
    if rooms is not None:  # проверка конкретных аудиторий
        for r in rooms:
            print(f"  {'свободна' if r['free'] else 'занята  '}  {r['room']}")
        return
    free = data.get("free_rooms") or []
    campus = f" в корпусе {data['campus']}" if data.get("campus") else ""
    print(f"Свободно{campus}: {data.get('total_free', len(free))}")
    for r in free:
        print(f"  {r['room']}  ({r.get('campus') or '?'})")
    if data.get("truncated"):
        print("  … список обрезан, уточните корпус (--campus)")


# --- сессия ---


@asynccontextmanager
async def _session(store) -> AsyncIterator[dict]:
    """Локальная сессия с автосохранением (как в MCP-local)."""
    import json as _j

    from pymirea.exceptions import MireaSessionExpired
    from pymirea.store import NotLoggedIn
    from pymirea.tokens import ensure_fresh_token

    try:
        stored = store.load()
    except NotLoggedIn as e:
        raise S.ServiceError(f"{e} Выполните `mirea-connect login`.") from e
    cookies = stored.cookies
    before = _j.dumps(cookies, sort_keys=True, default=str)
    try:
        await ensure_fresh_token(cookies, buffer_s=60)
    except MireaSessionExpired as e:
        store.clear()
        raise S.SessionExpired("Сессия МИРЭА истекла. Выполните `mirea-connect login`.") from e
    try:
        yield cookies
    finally:
        if _j.dumps(cookies, sort_keys=True, default=str) != before:
            store.save(cookies)


# --- запуск одной команды ---


async def _personal(store, op: Callable[[dict], Awaitable[Any]]) -> Any:
    async with _session(store) as cookies:
        return await op(cookies)


async def run(args, store) -> int:
    use_json = getattr(args, "json", False)

    async def show(coro: Awaitable[Any], render: Callable[[Any], None]) -> int:
        data = await coro
        _json(data) if use_json else render(data)
        return 0

    cmd = args.cmd
    if cmd == "schedule":
        return await show(_personal(store, lambda c: S.my_schedule(c, args.days)), _render_lessons)
    if cmd == "grades":
        return await show(_personal(store, S.grades), _render_grades)
    if cmd == "attendance":
        return await show(_personal(store, lambda c: S.attendance(c, args.discipline)), _render_attendance)
    if cmd == "acs":
        return await show(_personal(store, S.acs_today), _render_acs)
    if cmd == "sdo":
        return await show(_personal(store, lambda c: S.sdo_overview(c, deadline_limit=args.limit)), _render_sdo)
    if cmd == "search":
        return await show(S.search(args.kind, args.query), _render_search)
    if cmd == "timetable":
        return await show(S.timetable(args.kind, args.query, args.days, args.date), _render_timetable)
    if cmd == "free-rooms":
        return await show(
            S.free_rooms(args.rooms or None, args.date, args.start, args.end, args.campus), _render_free_rooms
        )
    raise S.ServiceError(f"Неизвестная команда {cmd}")


def add_subparsers(sub, common) -> None:
    """Зарегистрировать data-команды в argparse ``sub`` (с родителем ``common``)."""
    kinds = ("group", "teacher", "classroom")

    s = sub.add_parser("schedule", parents=[common], help="своё расписание из Пульса")
    s.add_argument("-d", "--days", type=int, default=7)

    sub.add_parser("grades", parents=[common], help="баллы БРС")

    s = sub.add_parser("attendance", parents=[common], help="посещаемость по дисциплине")
    s.add_argument("discipline", help="discipline_id или часть названия")

    sub.add_parser("acs", parents=[common], help="турникеты за сегодня")

    s = sub.add_parser("sdo", parents=[common], help="СДО: курсы, дедлайны, оценки")
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("search", parents=[common], help="поиск группы/преподавателя/аудитории")
    s.add_argument("kind", choices=kinds)
    s.add_argument("query")

    s = sub.add_parser("timetable", parents=[common], help="публичное расписание (вход не нужен)")
    s.add_argument("kind", choices=kinds)
    s.add_argument("query")
    s.add_argument("-d", "--days", type=int, default=7)
    s.add_argument("--date", help="начальная дата YYYY-MM-DD")

    s = sub.add_parser("free-rooms", parents=[common], help="свободные аудитории в интервал")
    s.add_argument("start", help="начало HH:MM")
    s.add_argument("end", help="конец HH:MM")
    s.add_argument("--date", help="дата YYYY-MM-DD (по умолчанию сегодня)")
    s.add_argument("--campus", help="корпус, например В-78")
    s.add_argument("rooms", nargs="*", help="конкретные аудитории; без них — список свободных")


DATA_COMMANDS = {"schedule", "grades", "attendance", "acs", "sdo", "search", "timetable", "free-rooms"}
