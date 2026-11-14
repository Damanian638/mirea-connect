"""Тесты для журнала преподавателя в Пульсе (пары, студенты, выставление отметок)."""

from __future__ import annotations

import secrets
from unittest.mock import AsyncMock, patch

import pytest
from pymirea import Config, _settings, configure

from mirea_connect import services as S
from mirea_connect.pulse import MireaGrades


@pytest.fixture(autouse=True)
def configured():
    previous = _settings._cfg
    configure(Config(session_keys=secrets.token_urlsafe(32)))
    yield
    _settings._cfg = previous


def test_pulse_protobuf_encoding_and_decoding():
    api = MireaGrades(session_cookies={"Pulse.Auth.Cookie": "fake"})

    # 1. Date request
    enc_date = api._encode_get_my_lessons(2026, 11, 14)
    assert enc_date.startswith(b"\x0a")

    # 2. Mock lesson response
    lid = b"lesson-123"
    raw_lid = b"\x0a" + api._encode_varint(len(lid)) + lid

    disc_name = "Цифровое телевидение".encode("utf-8")
    raw_disc = b"\x12" + api._encode_varint(len(disc_name)) + disc_name
    disc_field = b"\x1a" + api._encode_varint(len(raw_disc)) + raw_disc

    vlog_title = "ИКБО-01-23".encode("utf-8")
    vlog_id = b"vlog-456"
    vlog_body = b"\x0a" + api._encode_varint(len(vlog_id)) + vlog_id + b"\x12" + api._encode_varint(len(vlog_title)) + vlog_title
    vlog_field = b"\x42" + api._encode_varint(len(vlog_body)) + vlog_body

    # Timestamp for 09:00 MSK (06:00 UTC): epoch = 1794636000
    epoch = 1794636000
    ts_body = b"\x08" + api._encode_varint(epoch)
    start_field = b"\x2a" + api._encode_varint(len(ts_body)) + ts_body

    lesson_msg = raw_lid + disc_field + vlog_field + start_field
    mock_resp = b"\x12" + api._encode_varint(len(lesson_msg)) + lesson_msg

    lessons = api._parse_my_lessons(mock_resp)
    assert len(lessons) == 1
    assert lessons[0]["id"] == "lesson-123"
    assert lessons[0]["discipline"] == "Цифровое телевидение"
    assert lessons[0]["visiting_logs"] == [{"id": "vlog-456", "title": "ИКБО-01-23"}]
    assert lessons[0]["start_epoch"] == epoch

    # 3. Attendance for lesson
    req_att = api._encode_get_attendance_for_lesson("lesson-123", "vlog-456")
    assert b"lesson-123" in req_att and b"vlog-456" in req_att

    # Mock student in attendance response
    s_sid = b"std-789"
    s_first = "Иван".encode("utf-8")
    s_last = "Иванов".encode("utf-8")
    s_mid = "Иванович".encode("utf-8")

    b1 = b"\x0a" + api._encode_varint(len(s_sid)) + s_sid
    b2 = b"\x12" + api._encode_varint(len(s_first)) + s_first
    b3 = b"\x1a" + api._encode_varint(len(s_last)) + s_last
    b_mid_val = b"\x0a" + api._encode_varint(len(s_mid)) + s_mid
    b6 = b"\x32" + api._encode_varint(len(b_mid_val)) + b_mid_val

    # present status
    b_aid = b"\x0a\x05att-1"
    b_present = b"\x12\x00"
    b_da = b"\x3a" + api._encode_varint(len(b_present)) + b_present
    b_existing = b_aid + b_da
    b7 = b"\x3a" + api._encode_varint(len(b_existing)) + b_existing

    student_msg = b1 + b2 + b3 + b6 + b7
    resp_att = b"\x0a" + api._encode_varint(len(student_msg)) + student_msg

    students = api._parse_attendance_for_lesson(resp_att)
    assert len(students) == 1
    assert students[0]["student_id"] == "std-789"
    assert students[0]["fullname"] == "Иванов Иван Иванович"
    assert students[0]["status"] == "present"

    # 4. Update attendance encoding & decoding
    enc_update = api._encode_update_attendance("lesson-123", [{"student_id": "std-789", "attend_type": 3}])
    assert b"lesson-123" in enc_update and b"std-789" in enc_update

    # Response with result = 1 (OK)
    ok_resp = b"\x10\x01"
    ok, err = api._parse_update_attendance_response(ok_resp)
    assert ok is True and err is None

    # Response with non-zero error
    fail_resp = b"\x10\x02"
    ok, err = api._parse_update_attendance_response(fail_resp)
    assert ok is False and err is not None


@pytest.mark.asyncio
async def test_teacher_lessons_service():
    mock_lessons = [
        {
            "id": "l-1",
            "discipline": "Телевидение",
            "lesson_type": "Лекция",
            # 09:00 MSK (UTC+3) -> 06:00 UTC on 2026-11-14 = 1794636000
            "start_epoch": 1794636000,
            "end_epoch": 1794641400,
            "auditoriums": ["А-101"],
            "visiting_logs": [{"id": "v-1", "title": "ИКБО-01-23"}],
        }
    ]

    with patch.object(MireaGrades, "get_my_lessons_for_date", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = (mock_lessons, None)
        res = await S.teacher_lessons({"Pulse.Auth.Cookie": "fake"}, date="2026-11-14")
        assert res["count"] == 1
        l = res["lessons"][0]
        assert l["order"] == 1
        assert l["pair_number"] == 1
        assert l["time"] == "09:00 - 10:30"
        assert l["discipline"] == "Телевидение"
        assert l["groups"] == ["ИКБО-01-23"]
        assert l["lesson_id"] == "l-1"


@pytest.mark.asyncio
async def test_teacher_lesson_students_service():
    mock_students = [
        {
            "student_id": "s-1",
            "firstname": "Иван",
            "lastname": "Иванов",
            "middlename": "Иванович",
            "fullname": "Иванов Иван Иванович",
            "attendance_id": "att-1",
            "status": "present",
            "human_id": "h-1",
        }
    ]

    with patch.object(MireaGrades, "get_attendance_for_lesson", new_callable=AsyncMock) as mock_att:
        mock_att.return_value = (mock_students, None)
        res = await S.teacher_lesson_students(
            {"Pulse.Auth.Cookie": "fake"}, lesson_id="l-1", visiting_log_id="v-1"
        )
        assert res["total_students"] == 1
        assert res["students"][0]["fullname"] == "Иванов Иван Иванович"
        assert res["students"][0]["status"] == "present"


@pytest.mark.asyncio
async def test_teacher_mark_attendance_by_name_and_by_id():
    mock_students = [
        {
            "student_id": "s-1",
            "fullname": "Иванов Иван Иванович",
            "status": "none",
            "attendance_id": "",
        },
        {
            "student_id": "s-2",
            "fullname": "Петров Петр Сергеевич",
            "status": "none",
            "attendance_id": "",
        },
    ]

    with patch("mirea_connect.services.teacher_lesson_students", new_callable=AsyncMock) as mock_roster, \
         patch.object(MireaGrades, "update_attendance", new_callable=AsyncMock) as mock_update:
        mock_roster.return_value = {"students": mock_students}
        mock_update.return_value = (True, None)

        # 1. По ФИО и фамилии
        marks = [
            {"name": "Иванов Иван", "status": "present"},
            {"student": "Петров", "status": "absent"},
        ]
        res = await S.teacher_mark_attendance({"Pulse.Auth.Cookie": "fake"}, "l-1", marks, "v-1")
        assert res["success"] is True
        assert res["updated_count"] == 2
        assert res["marks"][0]["student_id"] == "s-1"
        assert res["marks"][0]["status"] == "present"
        assert res["marks"][1]["student_id"] == "s-2"
        assert res["marks"][1]["status"] == "absent"

        mock_update.assert_called_once_with(
            "l-1",
            [
                {"student_id": "s-1", "attend_type": 3},
                {"student_id": "s-2", "attend_type": 1},
            ],
        )


@pytest.mark.asyncio
async def test_teacher_mark_attendance_ambiguous_name():
    mock_students = [
        {"student_id": "s-1", "fullname": "Иванов Иван", "status": "none"},
        {"student_id": "s-2", "fullname": "Иванов Алексей", "status": "none"},
    ]
    with patch("mirea_connect.services.teacher_lesson_students", new_callable=AsyncMock) as mock_roster:
        mock_roster.return_value = {"students": mock_students}
        with pytest.raises(S.ServiceError, match="несколько студентов"):
            await S.teacher_mark_attendance(
                {"Pulse.Auth.Cookie": "fake"}, "l-1", [{"name": "Иванов", "status": "present"}]
            )


@pytest.mark.asyncio
async def test_teacher_mark_attendance_unknown_name():
    mock_students = [
        {"student_id": "s-1", "fullname": "Иванов Иван", "status": "none"},
    ]
    with patch("mirea_connect.services.teacher_lesson_students", new_callable=AsyncMock) as mock_roster:
        mock_roster.return_value = {"students": mock_students}
        with pytest.raises(S.ServiceError, match="не найден"):
            await S.teacher_mark_attendance(
                {"Pulse.Auth.Cookie": "fake"}, "l-1", [{"name": "Сидоров", "status": "present"}]
            )


@pytest.mark.asyncio
async def test_mcp_teacher_tools(tmp_path):
    from pymirea.store import SessionStore

    from mirea_connect.local import build_local

    store = SessionStore(tmp_path / "home")
    store.configure()
    store.save({"Pulse.Auth.Cookie": "fake"}, login="teacher@mirea.ru")

    mcp, _ = build_local(store)

    with patch("mirea_connect.services.teacher_lessons", new_callable=AsyncMock) as mock_tl, \
         patch("mirea_connect.services.teacher_lesson_students", new_callable=AsyncMock) as mock_ts, \
         patch("mirea_connect.services.teacher_mark_attendance", new_callable=AsyncMock) as mock_tm:
        mock_tl.return_value = {"count": 1, "lessons": [{"lesson_id": "l-1", "discipline": "Test"}]}
        mock_ts.return_value = {"total_students": 1, "students": [{"student_id": "s-1", "fullname": "Иванов"}]}
        mock_tm.return_value = {"success": True, "updated_count": 1}

        res_l = await mcp.call_tool("my_teacher_lessons", {})
        txt_l = "".join(getattr(c, "text", "") for c in res_l.content)
        assert "Test" in txt_l

        res_s = await mcp.call_tool("my_lesson_students", {"lesson_id": "l-1"})
        txt_s = "".join(getattr(c, "text", "") for c in res_s.content)
        assert "Иванов" in txt_s

        res_m = await mcp.call_tool(
            "my_mark_lesson_attendance",
            {"lesson_id": "l-1", "marks": [{"student": "Иванов", "status": "present"}]},
        )
        txt_m = "".join(getattr(c, "text", "") for c in res_m.content)
        assert '"success": true' in txt_m.lower()
