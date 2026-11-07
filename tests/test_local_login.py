"""Локальная страница входа: сроки, повторный запуск, отказ сервера, чистый stdout."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import sys

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pymirea import _settings
from pymirea.store import SessionStore

from mirea_connect import local


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("PYMIREA_SESSION_KEYS", raising=False)
    prev = _settings._cfg
    _settings._cfg = None
    yield SessionStore(tmp_path / "home")
    _settings._cfg = prev


def _url(res) -> str:
    return json.loads("".join(getattr(c, "text", "") for c in res.content))["url"]


async def _alive(url: str) -> bool:
    try:
        async with httpx.AsyncClient() as c:
            return (await c.get(url, timeout=2)).status_code == 200
    except httpx.HTTPError:
        return False


async def test_old_expiry_timer_does_not_close_a_new_login_page(store, monkeypatch):
    mcp, page = local.build_local(store)
    try:
        monkeypatch.setattr(local, "LOGIN_PAGE_TTL_S", 0.5)
        first = _url(await mcp.call_tool("login", {"open_browser": False}))
        await page.stop()  # например, вход завершён
        monkeypatch.setattr(local, "LOGIN_PAGE_TTL_S", 60)
        second = _url(await mcp.call_tool("login", {"open_browser": False}))
        assert second != first
        await asyncio.sleep(0.9)  # срок первой страницы прошёл
        assert await _alive(second), "таймер первой страницы закрыл новую"
    finally:
        await page.stop()


async def test_page_expires(store, monkeypatch):
    mcp, page = local.build_local(store)
    monkeypatch.setattr(local, "LOGIN_PAGE_TTL_S", 0.3)
    try:
        url = _url(await mcp.call_tool("login", {"open_browser": False}))
        assert await _alive(url)
        await asyncio.sleep(0.8)
        assert not await _alive(url)
    finally:
        await page.stop()


async def test_port_conflict_gives_error_and_keeps_server_alive(store, monkeypatch):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    monkeypatch.setattr(local, "_free_port", lambda: busy.getsockname()[1])
    mcp, page = local.build_local(store)
    try:
        with pytest.raises(ToolError, match="страницу входа"):
            await mcp.call_tool("login", {"open_browser": False})
        # процесс MCP жив, остальные инструменты работают
        res = await mcp.call_tool("whoami", {})
        assert "logged_in" in "".join(getattr(c, "text", "") for c in res.content)
    finally:
        busy.close()
        await page.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows браузер открывается через os.startfile")
async def test_opening_browser_does_not_write_to_stdout(store, monkeypatch, tmp_path, capfd):
    """В stdio stdout — канал протокола: вывод xdg-open/браузера сломал бы JSON-RPC."""
    browser = tmp_path / "noisy-browser"
    browser.write_text("#!/bin/sh\necho 'Opening in existing browser session.'\necho warn >&2\n", encoding="utf-8")
    browser.chmod(browser.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("BROWSER", str(browser))
    monkeypatch.setattr(sys, "platform", "linux")
    mcp, page = local.build_local(store)
    try:
        res = await mcp.call_tool("login", {"open_browser": True})
        data = json.loads("".join(getattr(c, "text", "") for c in res.content))
        await asyncio.sleep(0.5)  # дать процессу браузера отработать
        assert data["browser_opened"] is True
        out = capfd.readouterr().out
        assert "Opening in existing" not in out and out == ""
    finally:
        await page.stop()


def test_open_browser_without_any_opener_returns_false(monkeypatch):
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(local.shutil, "which", lambda name: None)
    assert local._open_browser("http://127.0.0.1:1/x") is False


def test_open_browser_on_windows_uses_startfile(monkeypatch):
    opened = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "startfile", lambda url: opened.append(url), raising=False)
    assert local._open_browser("http://127.0.0.1:1/x") is True and opened == ["http://127.0.0.1:1/x"]
