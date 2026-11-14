"""Совместимость с текущей cookie Пульса и проверка авторизации через его API."""

from __future__ import annotations

from pymirea.grades import MireaGrades as _MireaGrades

PULSE_SESSION_COOKIES = ("Pulse.Auth.Cookie", ".AspNetCore.Cookies")
GET_ME_URL = "https://pulse.mirea.ru/rtu_tc.rtu_attend.app.UserService/GetMeInfo"
GET_MY_LESSONS_URL = "https://pulse.mirea.ru/rtu_tc.attendance.api.LessonService/GetMyLessonsForDate"
GET_ATTENDANCE_FOR_LESSON_URL = "https://pulse.mirea.ru/rtu_tc.attendance.api.AttendanceService/GetAttendanceForLesson"
UPDATE_ATTENDANCE_URL = "https://pulse.mirea.ru/rtu_tc.attendance.api.AttendanceService/UpdateAttendance"


def has_pulse_session(cookies) -> bool:
    return any(cookies.get(name) for name in PULSE_SESSION_COOKIES)


async def verify_pulse_session(client) -> bool:
    # Поля GetMeInfoRequest: app_url (1), remember_me (3).
    origin = b"https://pulse.mirea.ru"
    request = b"\x0a" + _MireaGrades._encode_varint(len(origin)) + origin + b"\x18\x01"
    response = await client.post(
        GET_ME_URL,
        content=_MireaGrades._grpc_web_frame(request),
        headers={
            "Content-Type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-requested-with": "XMLHttpRequest",
            "Origin": origin.decode(),
            "Referer": origin.decode() + "/",
            "pulse-app-type": "pulse-app",
        },
        follow_redirects=False,
    )
    if response.status_code != 200 or "application/grpc-web" not in response.headers.get("content-type", ""):
        return False
    body = response.content
    if "grpc-web-text" in response.headers.get("content-type", ""):
        body = _MireaGrades._try_decode_grpc_web_text(body) or b""
    messages, trailers = _MireaGrades._parse_grpc_web_frames(body)
    if trailers.get("grpc-status", response.headers.get("grpc-status")) != "0":
        return False
    # GetMeInfoResponse.result — oneof: authorized (1), not_authorized (2).
    # Проверяем вариант ответа, не выводя и не сохраняя профиль пользователя.
    if len(messages) != 1 or not messages[0].startswith(b"\x0a"):
        return False
    try:
        size, offset = _MireaGrades._decode_varint(messages[0], 1)
    except (IndexError, ValueError):
        return False
    return size > 0 and offset + size == len(messages[0])


class MireaGrades(_MireaGrades):
    async def _ensure_aspnet_cookie(self) -> tuple[bool, str | None]:
        # pymirea пока проверяет только прежнее имя cookie. Оба варианта
        # передаются штатным HTTP-клиентом вместе с фрагментами больших cookies.
        for name in PULSE_SESSION_COOKIES:
            for c in getattr(self.client.cookies, "jar", []):
                if c.name == name and c.value:
                    self.session_cookies[name] = c.value
                    return True, None
            existing = self.session_cookies.get(name)
            if existing:
                return True, None
        return await super()._ensure_aspnet_cookie()

    def _encode_get_my_lessons(self, year: int, month: int, day: int) -> bytes:
        date_msg = (
            b"\x08" + self._encode_varint(year)
            + b"\x10" + self._encode_varint(month)
            + b"\x18" + self._encode_varint(day)
        )
        return b"\x0a" + self._encode_varint(len(date_msg)) + date_msg

    def _parse_named(self, buf: bytes) -> tuple[str, str]:
        p, nid, title = 0, "", ""
        while p < len(buf):
            key, p = self._decode_varint(buf, p)
            f, wt = key >> 3, key & 0x7
            if wt == 2:
                raw, p = self._read_length_delimited(buf, p)
                s = raw.decode("utf-8", errors="ignore")
                if f == 1:
                    nid = s
                elif f == 2:
                    title = s
            else:
                p = self._skip_field(buf, p, wt)
        return nid, title

    def _parse_single_lesson_info(self, data: bytes) -> dict:
        lesson = {
            "id": "",
            "discipline": "",
            "discipline_id": "",
            "lesson_type": "",
            "start_epoch": 0,
            "end_epoch": 0,
            "auditoriums": [],
            "visiting_logs": [],
        }
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if wt == 2:
                raw, p = self._read_length_delimited(data, p)
                if field == 1:
                    lesson["id"] = raw.decode("utf-8", errors="ignore")
                elif field == 2:
                    _, name = self._parse_named(raw)
                    if name:
                        lesson["auditoriums"].append(name)
                elif field == 3:
                    did, title = self._parse_named(raw)
                    lesson["discipline_id"] = did
                    lesson["discipline"] = title
                elif field == 4:
                    _, name = self._parse_named(raw)
                    lesson["lesson_type"] = name
                elif field == 5:
                    lesson["start_epoch"] = int(self._parse_timestamp_msg(raw) or 0)
                elif field == 6:
                    lesson["end_epoch"] = int(self._parse_timestamp_msg(raw) or 0)
                elif field == 8:
                    lid, title = self._parse_named(raw)
                    if lid:
                        lesson["visiting_logs"].append({"id": lid, "title": title})
            else:
                p = self._skip_field(data, p, wt)
        return lesson

    def _parse_my_lessons(self, data: bytes) -> list[dict]:
        lessons = []
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 2 and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                lesson = self._parse_single_lesson_info(raw)
                if lesson.get("id"):
                    lessons.append(lesson)
            else:
                p = self._skip_field(data, p, wt)
        return lessons

    def _encode_get_attendance_for_lesson(self, lesson_id: str, visiting_log_id: str) -> bytes:
        b_lesson = lesson_id.encode("utf-8")
        part1 = b"\x0a" + self._encode_varint(len(b_lesson)) + b_lesson
        b_log = visiting_log_id.encode("utf-8")
        part2 = b"\x12" + self._encode_varint(len(b_log)) + b_log
        return part1 + part2

    def _parse_existing_attend(self, buf: bytes) -> tuple[str, str]:
        aid = ""
        status = "none"
        p = 0
        while p < len(buf):
            key, p = self._decode_varint(buf, p)
            field = key >> 3
            wt = key & 0x7
            if wt == 2:
                raw, p = self._read_length_delimited(buf, p)
                if field == 1:
                    aid = raw.decode("utf-8", errors="ignore")
                elif field == 7:
                    p2 = 0
                    while p2 < len(raw):
                        k2, p2 = self._decode_varint(raw, p2)
                        f2, wt2 = k2 >> 3, k2 & 0x7
                        if wt2 == 2:
                            _, p2 = self._read_length_delimited(raw, p2)
                            if f2 == 1:
                                status = "absent"
                            elif f2 == 2:
                                status = "present"
                            elif f2 == 3:
                                status = "excused"
                        else:
                            p2 = self._skip_field(raw, p2, wt2)
            else:
                p = self._skip_field(buf, p, wt)
        return aid, status

    def _parse_single_student(self, data: bytes) -> dict:
        student = {
            "student_id": "",
            "firstname": "",
            "lastname": "",
            "middlename": "",
            "fullname": "",
            "attendance_id": "",
            "status": "none",
            "human_id": "",
        }
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if wt == 2:
                raw, p = self._read_length_delimited(data, p)
                if field == 1:
                    student["student_id"] = raw.decode("utf-8", errors="ignore")
                elif field == 2:
                    student["firstname"] = raw.decode("utf-8", errors="ignore")
                elif field == 3:
                    student["lastname"] = raw.decode("utf-8", errors="ignore")
                elif field == 6:
                    mid = self._parse_string_field(raw, 1)
                    if mid:
                        student["middlename"] = mid
                elif field == 7:
                    aid, status = self._parse_existing_attend(raw)
                    student["attendance_id"] = aid
                    student["status"] = status
                elif field == 8:
                    student["status"] = "none"
                elif field == 9:
                    student["human_id"] = raw.decode("utf-8", errors="ignore")
            else:
                p = self._skip_field(data, p, wt)
        fn = " ".join(f for f in (student["lastname"], student["firstname"], student["middlename"]) if f)
        student["fullname"] = fn
        return student

    def _parse_attendance_for_lesson(self, data: bytes) -> list[dict]:
        students = []
        p = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 1 and wt == 2:
                raw, p = self._read_length_delimited(data, p)
                student = self._parse_single_student(raw)
                if student.get("student_id"):
                    students.append(student)
            else:
                p = self._skip_field(data, p, wt)
        return students

    def _encode_update_attendance(self, lesson_id: str, records: list[dict]) -> bytes:
        out = bytearray()
        b_lesson = lesson_id.encode("utf-8")
        out.extend(b"\x12")
        out.extend(self._encode_varint(len(b_lesson)))
        out.extend(b_lesson)
        for rec in records:
            attendance_id = rec.get("attendance_id") or ""
            student_id = rec.get("student_id") or ""
            attend_type = int(rec.get("attend_type", 3))
            rec_buf = bytearray()
            if attendance_id:
                b_aid = attendance_id.encode("utf-8")
                rec_buf.extend(b"\x22")
                rec_buf.extend(self._encode_varint(len(b_aid)))
                rec_buf.extend(b_aid)
            elif student_id:
                b_sid = student_id.encode("utf-8")
                rec_buf.extend(b"\x1a")
                rec_buf.extend(self._encode_varint(len(b_sid)))
                rec_buf.extend(b_sid)
            if attend_type != 0:
                rec_buf.extend(b"\x28")
                rec_buf.extend(self._encode_varint(attend_type))
            out.extend(b"\x1a")
            out.extend(self._encode_varint(len(rec_buf)))
            out.extend(rec_buf)
        return bytes(out)

    def _parse_update_attendance_response(self, data: bytes) -> tuple[bool, str | None]:
        p = 0
        result = 0
        while p < len(data):
            key, p = self._decode_varint(data, p)
            field = key >> 3
            wt = key & 0x7
            if field == 2 and wt == 0:
                result, p = self._decode_varint(data, p)
            else:
                p = self._skip_field(data, p, wt)
        if result == 1:
            return True, None
        _err_labels = {
            2: "Ошибка при обновлении отметки",
            3: "У пользователя нет журнала для этого занятия",
            4: "Отметка уже изменилась на сервере (обновите ведомость)",
            5: "Занятие во внеплановом режиме",
            6: "Староста не может изменить отметку по правилам БРС",
            7: "Журнал находится в архиве",
            8: "Занятие ещё не началось (в будущем)",
        }
        return False, _err_labels.get(result, f"Ошибка обновления посещаемости (код {result})")

    async def get_my_lessons_for_date(self, year: int, month: int, day: int) -> tuple[list[dict] | None, str | None]:
        ok, err = await self._ensure_aspnet_cookie()
        if not ok:
            return None, err or "Сессия истекла. Перелогиньтесь в МИРЭА."
        payload = self._encode_get_my_lessons(year, month, day)
        resp, err = await self._grpc_unary(GET_MY_LESSONS_URL, payload)
        if err or resp is None:
            return None, err or "Не удалось получить список занятий"
        return self._parse_my_lessons(resp), None

    async def get_attendance_for_lesson(self, lesson_id: str, visiting_log_id: str) -> tuple[list[dict] | None, str | None]:
        ok, err = await self._ensure_aspnet_cookie()
        if not ok:
            return None, err or "Сессия истекла. Перелогиньтесь в МИРЭА."
        payload = self._encode_get_attendance_for_lesson(lesson_id, visiting_log_id)
        resp, err = await self._grpc_unary(GET_ATTENDANCE_FOR_LESSON_URL, payload)
        if err or resp is None:
            return None, err or "Не удалось получить посещаемость занятия"
        return self._parse_attendance_for_lesson(resp), None

    async def update_attendance(self, lesson_id: str, records: list[dict]) -> tuple[bool, str | None]:
        if not records:
            return True, None
        ok, err = await self._ensure_aspnet_cookie()
        if not ok:
            return False, err or "Сессия истекла. Перелогиньтесь в МИРЭА."
        payload = self._encode_update_attendance(lesson_id, records)
        resp, err = await self._grpc_unary(UPDATE_ATTENDANCE_URL, payload)
        if err:
            return False, err
        return self._parse_update_attendance_response(resp or b"")
