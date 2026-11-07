"""Совместимость с текущей cookie Пульса и проверка авторизации через его API."""

from __future__ import annotations

from pymirea.grades import MireaGrades as _MireaGrades

PULSE_SESSION_COOKIES = ("Pulse.Auth.Cookie", ".AspNetCore.Cookies")
GET_ME_URL = "https://pulse.mirea.ru/rtu_tc.rtu_attend.app.UserService/GetMeInfo"


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
            existing = self.client.cookies.get(name)
            if existing:
                self.session_cookies[name] = existing
                return True, None
        return await super()._ensure_aspnet_cookie()
