"""Локальный режим: вход через страницу на 127.0.0.1, сессия на диске, установщик."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from pymirea import _settings
from pymirea.auth import AuthChallenge, AuthResult
from pymirea.store import SessionStore

from mirea_connect import installer, services
from mirea_connect.auth import MireaAuth
from mirea_connect.local import build_local


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("PYMIREA_SESSION_KEYS", raising=False)
    prev = _settings._cfg
    _settings._cfg = None
    yield SessionStore(tmp_path / "home")
    _settings._cfg = prev


def _text(res) -> str:
    return "".join(getattr(c, "text", "") for c in res.content)


async def test_sdo_failure_does_not_delete_pulse_session(store, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError
    from pymirea.sdo import SdoResult

    class UnavailableSDO:
        def __init__(self, session_cookies):
            pass

        async def get_courses(self):
            return SdoResult(False, "Сессия СДО не найдена. Перелогиньтесь в МИРЭА.")

        async def get_deadlines(self, **kwargs):
            return await self.get_courses()

        async def get_grades(self, **kwargs):
            return await self.get_courses()

        async def close(self):
            pass

    monkeypatch.setattr(services, "MireaSDO", UnavailableSDO)
    mcp, _ = build_local(store)
    store.save({"Pulse.Auth.Cookie": "valid-pulse"}, login="test@edu.mirea.ru")
    with pytest.raises(ToolError, match="Сессия Пульса сохранена"):
        await mcp.call_tool("my_sdo", {})
    assert store.load().cookies["Pulse.Auth.Cookie"] == "valid-pulse"


async def test_local_login_page_and_personal_tool(store, monkeypatch):
    ch = AuthChallenge(kind="otp", action_url="https://sso.mirea.ru/x", field_name="otp", hidden_fields={})

    async def fake_login(self, u, p):
        return AuthResult(success=False, message="otp", challenge=ch)

    async def fake_2fa(self, c, code, *, cookies=None):
        if code != "111111":
            return AuthResult(success=False, message="Неверный код", challenge=c)
        return AuthResult(success=True, message="ok", cookies={"K": "v"})

    async def fake_grades(cookies):
        assert cookies["K"] == "v"
        return {"subjects": [{"name": "Физика"}]}

    monkeypatch.setattr(MireaAuth, "login", fake_login)
    monkeypatch.setattr(MireaAuth, "complete_2fa", fake_2fa)
    monkeypatch.setattr(services, "grades", fake_grades)

    mcp, page = build_local(store)
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="login"):
        await mcp.call_tool("my_grades", {})

    res = await mcp.call_tool("login", {"open_browser": False})
    url = json.loads(_text(res))["url"]
    assert url.startswith("http://127.0.0.1:")
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(url)
            assert "Вход в МИРЭА" in r.text
            # без секрета в пути страница недоступна
            base = re.match(r"(http://127.0.0.1:\d+)", url).group(1)
            assert (await c.get(base + "/")).status_code == 404
            r = await c.post(url + "/login", data={"username": "me@edu.mirea.ru", "password": "pw"})
            assert "Код подтверждения" in r.text
            r = await c.post(url + "/otp", data={"code": "000000"})
            assert "Неверный код" in r.text
            r = await c.post(url + "/otp", data={"code": "111111"})
            assert "Готово" in r.text
    finally:
        await page.stop()

    assert store.load().login == "me@edu.mirea.ru"
    res = await mcp.call_tool("my_grades", {})
    assert "Физика" in _text(res)
    res = await mcp.call_tool("whoami", {})
    assert '"logged_in": true' in _text(res)
    await mcp.call_tool("delete_my_data", {})
    assert not store.exists()


def test_installer_writes_configs(tmp_path, monkeypatch):
    desktop = tmp_path / "Claude" / "claude_desktop_config.json"
    desktop.parent.mkdir()
    desktop.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}))
    cursor = tmp_path / ".cursor" / "mcp.json"
    cursor.parent.mkdir()
    monkeypatch.setattr(installer, "claude_desktop_config", lambda: desktop)
    monkeypatch.setattr(installer, "cursor_config", lambda: cursor)
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-hermes"))
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "no-openclaw"))

    assert installer.install(True) == 0
    d = json.loads(desktop.read_text())
    assert d["keep"] == 1 and "other" in d["mcpServers"]
    assert d["mcpServers"]["mirea"]["args"][-2:] == ["mirea_connect", "local"]
    assert "mirea" in json.loads(cursor.read_text())["mcpServers"]

    installer.install(False)
    assert "mirea" not in json.loads(desktop.read_text())["mcpServers"]
    assert "other" in json.loads(desktop.read_text())["mcpServers"]


def test_codex_toml_roundtrip():
    import tomllib

    src = 'model = "o4"\n\n[mcp_servers.other]\ncommand = "x"\n\n[mcp_servers.mirea]\ncommand = "old"\n\n[mcp_servers.mirea.env]\nA = "1"\n\n[profiles.p]\nmodel = "y"\n'
    out, changed = installer.edit_codex_toml(src, True)
    data = tomllib.loads(out)
    assert changed
    assert data["model"] == "o4" and data["profiles"]["p"]["model"] == "y"
    assert data["mcp_servers"]["other"]["command"] == "x"
    assert data["mcp_servers"]["mirea"]["args"][-2:] == ["mirea_connect", "local"]
    assert "env" not in data["mcp_servers"]["mirea"]
    out2, _ = installer.edit_codex_toml(out, True)
    assert out2.count("[mcp_servers.mirea]") == 1
    removed, changed = installer.edit_codex_toml(out2, False)
    assert changed and "mirea" not in tomllib.loads(removed)["mcp_servers"]
    assert installer.edit_codex_toml("", True)[0].startswith("[mcp_servers.mirea]")


def test_hermes_yaml_roundtrip():
    yaml = pytest.importorskip("yaml")
    src = (
        "model: hermes-4\n"
        "mcp_servers:\n"
        "    # comment\n"
        "    filesystem:\n"
        "        command: npx\n"
        "        args: [\"-y\", \"x\"]\n"
        "    mirea:\n"
        "        command: old\n"
        "        args: []\n"
        "toolsets:\n"
        "  - web\n"
    )
    out, changed = installer.edit_hermes_yaml(src, True)
    data = yaml.safe_load(out)
    assert changed and data["model"] == "hermes-4" and data["toolsets"] == ["web"]
    assert data["mcp_servers"]["filesystem"]["command"] == "npx"
    assert data["mcp_servers"]["mirea"]["args"][-2:] == ["mirea_connect", "local"]
    assert "# comment" in out
    out2, _ = installer.edit_hermes_yaml(out, True)
    assert out2.count("mirea:") == 1
    removed, changed = installer.edit_hermes_yaml(out2, False)
    assert changed and "mirea" not in yaml.safe_load(removed)["mcp_servers"]

    fresh, _ = installer.edit_hermes_yaml("model: x\n", True)
    assert yaml.safe_load(fresh)["mcp_servers"]["mirea"]["command"]
    empty, _ = installer.edit_hermes_yaml("mcp_servers: {}\n", True)
    assert "mirea" in yaml.safe_load(empty)["mcp_servers"]


def test_installer_codex_hermes_openclaw(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(installer, "claude_desktop_config", lambda: tmp_path / "nope" / "c.json")
    monkeypatch.setattr(installer, "cursor_config", lambda: tmp_path / "nope2" / "mcp.json")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text("model: x\n")
    monkeypatch.setattr(installer.shutil, "which", lambda n: "/bin/openclaw" if n == "openclaw" else None)

    class R:
        returncode = 0
        stdout = stderr = ""

    def fake_run(args, **kw):
        calls.append(args)
        return R()

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    installer.install(True)
    assert "[mcp_servers.mirea]" in (tmp_path / ".codex" / "config.toml").read_text()
    assert "mirea:" in (tmp_path / ".hermes" / "config.yaml").read_text()
    oc = [c for c in calls if c[0] == "/bin/openclaw"]
    assert oc and oc[0][1:4] == ["mcp", "set", "mirea"]
    assert json.loads(oc[0][4])["args"][-1] == "local"


def test_prompt_command(capsys):
    from mirea_connect.cli import main

    assert main(["prompt", "--json"]) == 0
    spec = json.loads(capsys.readouterr().out)
    assert spec["transport"] == "stdio" and spec["args"][-1] == "local"
    assert main(["prompt"]) == 0
    text = capsys.readouterr().out
    assert spec["command"] in text and "llms-install.md" in text and "пароль" in text


def test_menu_choose():
    answers = iter(["", "9", "abc", "2 3", "3"])
    ask = lambda q: next(answers)  # noqa: E731
    found = ["Claude Desktop", "Codex"]
    assert installer.choose(found, ask) == found  # Enter — все найденные
    assert installer.choose(found, ask) == ["Codex", installer.PROMPT_CHOICE]  # 9 и abc отклонены, потом «2 3»
    assert installer.choose(found, ask) == [installer.PROMPT_CHOICE]
    assert installer.choose([], lambda q: "") == [installer.PROMPT_CHOICE]


def test_install_prompt_choice_and_no_clients(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PYMIREA_HOME", str(tmp_path / "pm"))
    for var in ("CODEX_HOME", "HERMES_HOME", "OPENCLAW_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var))
    monkeypatch.setattr(installer, "claude_desktop_config", lambda: tmp_path / "x" / "c.json")
    monkeypatch.setattr(installer, "cursor_config", lambda: tmp_path / "y" / "mcp.json")
    monkeypatch.setattr(installer.shutil, "which", lambda n: None)

    installer.install(True, mode="all")  # клиентов нет → сразу промпт
    out = capsys.readouterr().out
    assert "Не нашёл ни одного известного клиента" in out and "llms-install.md" in out
    assert (tmp_path / "pm" / "agent-prompt.txt").read_text().startswith("Подключи к себе")

    installer.install(True, mode="prompt")
    assert "Вставьте этот промпт" in capsys.readouterr().out


def test_cli_data_commands(monkeypatch, capsys, tmp_path):
    """CLI-команды рендерят данные от services без сети."""
    import json as _json

    from mirea_connect import services
    from mirea_connect.cli import main

    monkeypatch.setenv("PYMIREA_HOME", str(tmp_path / "home"))

    async def fake_timetable(kind, query, days, date_from):
        return {
            "kind": kind,
            "name": query,
            "events": [{"start": "2030-01-10T09:00+03:00", "end": "2030-01-10T10:30+03:00", "summary": "Физика", "location": "А-9"}],
        }

    async def fake_free(rooms, date, start, end, campus=None):
        if rooms:
            return {"rooms": [{"room": r, "free": r == "А-9"} for r in rooms]}
        return {"campus": campus, "total_free": 1, "free_rooms": [{"room": "А-9", "campus": "В-78"}]}

    monkeypatch.setattr(services, "timetable", fake_timetable)
    monkeypatch.setattr(services, "free_rooms", fake_free)

    assert main(["timetable", "group", "ЭФБО-01-24", "--json"]) == 0
    assert _json.loads(capsys.readouterr().out)["events"][0]["summary"] == "Физика"

    assert main(["timetable", "group", "ЭФБО-01-24"]) == 0
    out = capsys.readouterr().out
    assert "Физика" in out and "09:00-10:30" in out

    assert main(["free-rooms", "10:40", "12:10", "А-9", "Б-1"]) == 0
    out = capsys.readouterr().out
    assert "свободна" in out and "занята" in out

    # личная команда без входа → понятная ошибка, не трейс
    assert main(["grades"]) == 1
    assert "login" in capsys.readouterr().err


def test_cli_cyrillic_output_survives_ascii_stdout(monkeypatch, tmp_path):
    """status не падает и выводит кириллицу даже при stdout с ascii-кодировкой (старая консоль)."""
    import io

    from mirea_connect.cli import main

    monkeypatch.setenv("PYMIREA_HOME", str(tmp_path / "home"))
    buf = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict", newline="")
    monkeypatch.setattr("sys.stdout", buf)
    rc = main(["status"])  # _force_utf8 должен перевести stdout в utf-8 → без UnicodeEncodeError
    buf.flush()
    out = buf.buffer.getvalue().decode("utf-8")
    assert rc == 1 and "Вход не выполнен" in out
