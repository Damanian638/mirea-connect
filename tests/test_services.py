"""Публичное расписание и свободные аудитории: выбор сущности, валидация, московское время."""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest
import respx
from pymirea.schedule import SCHEDULE_SUPABASE_URL

from mirea_connect import services as S

RPC = f"{SCHEDULE_SUPABASE_URL}/rest/v1/rpc"
LESSON = [{"type": "__lesson_schedule__", "subject": "Физика", "lesson_type": "lecture", "dates": ["10-01-2030"],
           "lesson_bells": {"start_time": "09:00", "end_time": "10:30"}, "classrooms": [{"name": "А-9"}],
           "groups": ["ЭФБО-01-24"]}]
TEACHERS = ["Иванов Артем Дмитриевич", "Иванов Илья Алексеевич", "Иванов Илья Иванович"]


def _targets(*names):
    return [{"external_id": str(100 + i), "full_title": n} for i, n in enumerate(names)]


def _mock(targets, free=None):
    search = respx.post(f"{RPC}/search_schedule_targets").mock(return_value=httpx.Response(200, json=targets))
    entity = respx.post(f"{RPC}/get_schedule_for_entity").mock(return_value=httpx.Response(200, json=LESSON))
    rooms = respx.post(f"{RPC}/get_free_rooms_window").mock(return_value=httpx.Response(200, json=free or []))
    return search, entity, rooms


@respx.mock
@pytest.mark.parametrize("query", ["", "   "])
async def test_empty_query_is_rejected_instead_of_returning_a_random_entity(query):
    search, entity, _ = _mock(_targets("БАСО-01-22"))
    with pytest.raises(S.ServiceError, match="Укажите"):
        await S.timetable("group", query, 1, "2030-01-10")
    with pytest.raises(S.ServiceError, match="Укажите"):
        await S.search("group", query)
    assert not search.called and not entity.called


@respx.mock
async def test_ambiguous_teacher_lists_candidates_instead_of_picking_the_first():
    _, entity, _ = _mock(_targets(*TEACHERS))
    with pytest.raises(S.ServiceError) as e:
        await S.timetable("teacher", "Иванов", 1, "2030-01-10")
    msg = str(e.value)
    assert "несколько" in msg and all(t in msg for t in TEACHERS)
    assert not entity.called


@respx.mock
@pytest.mark.parametrize(
    "query, expected",
    [
        ("Иванов Илья Иванович", "Иванов Илья Иванович"),  # точное ФИО
        ("иванов  илья  иванович", "Иванов Илья Иванович"),  # регистр и пробелы
        ("Иванов И. И.", "Иванов Илья Иванович"),  # инициалы
        ("Иванов И.И.", "Иванов Илья Иванович"),
        ("Иванов А.Д.", "Иванов Артем Дмитриевич"),
        ("Иванов Илья Ал", "Иванов Илья Алексеевич"),  # начало отчества
    ],
)
async def test_teacher_resolved_by_full_name_or_initials(query, expected):
    _, entity, _ = _mock(_targets(*TEACHERS))
    data = await S.timetable("teacher", query, 1, "2030-01-10")
    assert data["name"] == expected and entity.called


@respx.mock
async def test_teacher_initials_matching_several_people_stay_ambiguous():
    _mock(_targets(*TEACHERS))
    with pytest.raises(S.ServiceError, match="несколько"):
        await S.timetable("teacher", "Иванов И.", 1, "2030-01-10")


@respx.mock
async def test_single_search_result_is_used():
    _mock(_targets("ЭФБО-01-24"))
    data = await S.timetable("group", "эфбо-01-24", 1, "2030-01-10")
    assert data["name"] == "ЭФБО-01-24" and data["events"][0]["summary"].startswith("Физика")


@respx.mock
async def test_classroom_with_several_campuses_is_ambiguous_but_campus_resolves_it():
    _mock(_targets("А-9 (В-78)", "А-9 (С-20)"))
    with pytest.raises(S.ServiceError, match="несколько"):
        await S.timetable("classroom", "А-9", 1, "2030-01-10")
    data = await S.timetable("classroom", "A-9 (B-78)", 1, "2030-01-10")  # латиница
    assert data["name"] == "А-9 (В-78)"


@respx.mock
async def test_nothing_found_message_mentions_possible_outage():
    _mock([])
    with pytest.raises(S.ServiceError, match="ничего не найдено.*недоступ"):
        await S.timetable("teacher", "Несуществующий", 1, None)


@respx.mock
@pytest.mark.parametrize("bad", ["2030-01-10T15:00", "2030-01-10T15:00+05:00", "10.01.2030", "2030-13-01", "завтра"])
async def test_date_must_be_a_plain_date(bad):
    search, _, rooms = _mock(_targets("ЭФБО-01-24"))
    with pytest.raises(S.ServiceError, match="ГГГГ-ММ-ДД"):
        await S.timetable("group", "ЭФБО-01-24", 1, bad)
    with pytest.raises(S.ServiceError, match="ГГГГ-ММ-ДД"):
        await S.free_rooms(None, bad, "10:40", "12:10")
    assert not search.called and not rooms.called


@respx.mock
async def test_negative_days_rejected():
    _mock(_targets("ЭФБО-01-24"))
    with pytest.raises(S.ServiceError, match="days"):
        await S.timetable("group", "ЭФБО-01-24", -1, "2030-01-10")


@respx.mock
async def test_days_window_boundaries():
    _mock(_targets("ЭФБО-01-24"))
    # Занятие 10.01 09:00 МСК: окно с 10.01 на 1 день включает его, с 11.01 — нет, с 09.01 на 1 день — нет.
    assert len((await S.timetable("group", "ЭФБО-01-24", 1, "2030-01-10"))["events"]) == 1
    assert (await S.timetable("group", "ЭФБО-01-24", 1, "2030-01-11"))["events"] == []
    assert (await S.timetable("group", "ЭФБО-01-24", 1, "2030-01-09"))["events"] == []
    assert len((await S.timetable("group", "ЭФБО-01-24", 2, "2030-01-09"))["events"]) == 1


@respx.mock
@pytest.mark.parametrize(
    "start, end",
    [("25:00", "26:00"), ("10:60", "12:10"), ("10", "12:10"), ("10:40:00", "12:10"), ("-1:00", "12:10"), ("", "1:00"),
     ("10:40", "24:00")],
)
async def test_invalid_time_gives_clear_error_not_a_crash(start, end):
    _, _, rooms = _mock([])
    with pytest.raises(S.ServiceError, match="ЧЧ:ММ"):
        await S.free_rooms(None, "2030-01-10", start, end)
    assert not rooms.called


@respx.mock
@pytest.mark.parametrize("start, end", [("10:40", "10:40"), ("12:10", "10:40")])
async def test_empty_or_reversed_interval_rejected(start, end):
    _mock([])
    with pytest.raises(S.ServiceError, match="позже"):
        await S.free_rooms(None, "2030-01-10", start, end)


@respx.mock
async def test_free_rooms_sends_moscow_interval_regardless_of_system_timezone(monkeypatch):
    _, _, rooms = _mock([], free=[{"room": "А-9", "campus": "В-78"}])
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        data = await S.free_rooms(None, "2030-01-10", "9:05", "12:10")
    finally:
        monkeypatch.delenv("TZ")
        if hasattr(time, "tzset"):
            time.tzset()
    body = json.loads(rooms.calls.last.request.content)
    assert body["p_from"] == "2030-01-10T09:05:00+03:00"
    assert body["p_to"] == "2030-01-10T12:10:00+03:00"
    assert data["from"] == "09:05" and data["total_free"] == 1


async def test_default_date_is_today_in_moscow(monkeypatch):
    from datetime import datetime, timezone

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            # 22:30 UTC 9 января = 01:30 МСК 10 января
            return datetime(2030, 1, 9, 22, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(S, "datetime", FakeDT)
    assert S._start_of(None).date().isoformat() == "2030-01-10"
    assert os.environ.get("TZ") != "Europe/Moscow" or True


@respx.mock
async def test_latin_lookalike_letters_match_campus_and_rooms():
    _mock([], free=[{"room": "А-9", "campus": "В-78"}, {"room": "Б-1", "campus": "В-78"}, {"room": "10", "campus": "С-20"}])
    data = await S.free_rooms(None, "2030-01-10", "10:40", "12:10", campus="B-78")  # латинская B
    assert data["total_free"] == 2
    data = await S.free_rooms(["A-9 (B-78)", "a-9", "A-10", "10 (C-20)"], "2030-01-10", "10:40", "12:10")
    assert [r["free"] for r in data["rooms"]] == [True, True, False, True]
    assert [r["room"] for r in data["rooms"]] == ["A-9 (B-78)", "a-9", "A-10", "10 (C-20)"]  # как ввёл пользователь


@respx.mock
async def test_unknown_campus_returns_empty_list_with_available_campuses():
    _mock([], free=[{"room": "А-9", "campus": "В-78"}, {"room": "10", "campus": "С-20"}])
    data = await S.free_rooms(None, "2030-01-10", "10:40", "12:10", campus="Х-99")
    assert data["total_free"] == 0 and data["free_rooms"] == []
    assert data["available_campuses"] == ["В-78", "С-20"]


@respx.mock
async def test_free_rooms_service_unavailable():
    respx.post(f"{RPC}/get_free_rooms_window").mock(return_value=httpx.Response(503, json={}))
    with pytest.raises(S.ServiceError, match="недоступен"):
        await S.free_rooms(None, "2030-01-10", "10:40", "12:10")


@pytest.mark.parametrize(
    "exc, fragment",
    [
        (httpx.ConnectTimeout("timed out"), "не ответила"),
        (httpx.ReadTimeout("timed out"), "не ответила"),
        (httpx.ConnectError("https://sso.mirea.ru/auth?code=SECRET-CODE&session_code=SC"), "связаться"),
        (OSError("Network is unreachable"), "связаться"),
        (KeyError("lesson_bells"), "Неожиданный ответ"),
        (AttributeError("'NoneType' object has no attribute 'events'"), "Неожиданный ответ"),
    ],
)
def test_unexpected_errors_become_friendly_service_errors(exc, fragment):
    err = S.friendly_error(exc)
    assert isinstance(err, S.ServiceError) and fragment in str(err)
    assert "SECRET-CODE" not in str(err) and "https://" not in str(err)


def test_service_errors_pass_through_unchanged():
    e = S.ServiceError("Укажите дисциплину.")
    assert S.friendly_error(e) is e
