"""Complete Moodle's OIDC form_post callback using the same HTTP session."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pymirea.sdo import SDO_URL
from pymirea.sdo import MireaSDO as _MireaSDO

_SESSKEY = re.compile(r'"sesskey"\s*:\s*"([^"\s]{6,64})"')


class MireaSDO(_MireaSDO):
    async def _read_sesskey(self) -> str | None:
        response = await self.client.get(f"{SDO_URL}/my/")
        target = urlsplit(str(response.url))
        if (
            response.status_code != 200
            or (target.scheme, target.netloc) != ("https", "online-edu.mirea.ru")
            or not target.path.startswith("/my/")
        ):
            return None
        match = _SESSKEY.search(response.text)
        if not match:
            return None
        self._store_moodle_session()
        self._sesskey = match.group(1)
        return self._sesskey

    async def bootstrap_session(self) -> bool:
        self._sesskey = None
        try:
            response = await self.client.get(f"{SDO_URL}/auth/oidc/", follow_redirects=True)
            if response.status_code == 200 and response.url.host == "sso.mirea.ru":
                for form in BeautifulSoup(response.text, "html.parser").find_all("form"):
                    if str(form.get("method", "get")).lower() != "post":
                        continue
                    action = urljoin(str(response.url), str(form.get("action", "")))
                    target = urlsplit(action)
                    # Only forward authorization codes to Moodle's own callback.
                    if (
                        (target.scheme, target.netloc) != ("https", "online-edu.mirea.ru")
                        or target.path != "/auth/oidc/"
                    ):
                        continue
                    fields = {
                        str(inp["name"]): str(inp.get("value", ""))
                        for inp in form.find_all("input", attrs={"name": True})
                        if str(inp.get("type", "")).lower() == "hidden"
                    }
                    if not fields.get("code") or not fields.get("state"):
                        continue
                    await self.client.post(action, data=fields, follow_redirects=True)
                    break
            # MoodleSession is also issued to guests; verify the personal page.
            return bool(await self._read_sesskey())
        except httpx.HTTPError:
            return False

    async def _ensure_session(self) -> tuple[bool, str | None]:
        if await self._get_sesskey():
            return True, None
        return False, "Не удалось завершить вход в СДО через SSO. Сессия Пульса сохранена."

    async def _get_sesskey(self) -> str | None:
        if self._sesskey:
            return self._sesskey
        try:
            if await self._read_sesskey():
                return self._sesskey
        except httpx.HTTPError:
            return None
        if await self.bootstrap_session():
            return self._sesskey
        return None
