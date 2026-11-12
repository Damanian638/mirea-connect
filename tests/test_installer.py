"""Регистрация в MCP-клиентах: чужие настройки не теряются, ошибки дают ненулевой код."""

from __future__ import annotations

import json

import pytest

from mirea_connect import installer
from mirea_connect.cli import main


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Только Claude Desktop; остальные клиенты не найдены."""
    desktop = tmp_path / "Иван Петров" / "Claude" / "claude_desktop_config.json"  # пробел и кириллица в пути
    desktop.parent.mkdir(parents=True)
    monkeypatch.setattr(installer, "claude_desktop_config", lambda: desktop)
    monkeypatch.setattr(installer, "cursor_config", lambda: tmp_path / "no-cursor" / "mcp.json")
    monkeypatch.setattr(installer, "antigravity_config", lambda: tmp_path / "no-gemini" / "mcp_config.json")
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    for var in ("CODEX_HOME", "HERMES_HOME", "OPENCLAW_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var))
    monkeypatch.setenv("PYMIREA_HOME", str(tmp_path / "pm"))
    return desktop


OTHER = {"mcpServers": {"other": {"command": "x", "args": ["-y"]}}, "globalShortcut": "Ctrl+Space"}


def test_config_with_utf8_bom_keeps_other_servers(env):
    """Блокнот Windows сохраняет JSON с BOM — это не повод стирать чужие серверы."""
    env.write_bytes(b"\xef\xbb\xbf" + json.dumps(OTHER).encode())
    assert installer.install(True, mode="all") == 0
    data = json.loads(env.read_text(encoding="utf-8-sig"))
    assert data["mcpServers"]["other"] == OTHER["mcpServers"]["other"]
    assert data["globalShortcut"] == "Ctrl+Space"
    assert data["mcpServers"]["mirea"]["args"] == ["-m", "mirea_connect", "local"]


@pytest.mark.parametrize(
    "broken", ['{"mcpServers": {"other": {"command": "x"},}}', "{", '{"mcpServers": []}', "[1, 2]"]
)
def test_unreadable_config_is_left_untouched_and_install_fails(env, broken, capsys):
    env.write_text(broken, encoding="utf-8")
    assert installer.install(True, mode="all") != 0
    assert env.read_text(encoding="utf-8") == broken  # файл пользователя не изменён
    out = capsys.readouterr().out
    assert "Claude Desktop" in out and str(env) in out
    assert "Подключено" not in out


def test_reinstall_is_idempotent_and_keeps_settings(env):
    env.write_text(json.dumps(OTHER, indent=2), encoding="utf-8")
    assert installer.install(True, mode="all") == 0
    first = env.read_text(encoding="utf-8")
    assert installer.install(True, mode="all") == 0
    assert env.read_text(encoding="utf-8") == first
    data = json.loads(first)
    assert list(data["mcpServers"]) == ["other", "mirea"]
    assert env.with_suffix(".json.bak").exists()  # перед первой правкой сохранена копия


def test_uninstall_keeps_other_servers(env):
    env.write_text(json.dumps(OTHER), encoding="utf-8")
    installer.install(True, mode="all")
    assert installer.install(False) == 0
    assert json.loads(env.read_text(encoding="utf-8")) == OTHER


def test_uninstall_without_config_does_not_create_one(env):
    assert installer.install(False) == 0
    assert not env.exists()


def test_failing_client_makes_cli_exit_nonzero(env, monkeypatch, capsys):
    def boom(path, add):
        raise PermissionError(13, "Отказано в доступе", str(path))

    monkeypatch.setattr(installer, "_merge_json", boom)
    assert main(["install", "--all"]) != 0
    out = capsys.readouterr().out
    assert "Не удалось подключить" in out and "Claude Desktop" in out and "Подключено" not in out


def test_partial_failure_reports_both(env, tmp_path, monkeypatch, capsys):
    cursor = tmp_path / ".cursor" / "mcp.json"
    cursor.parent.mkdir()
    cursor.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(installer, "cursor_config", lambda: cursor)
    env.write_text("{", encoding="utf-8")
    assert installer.install(True, mode="all") != 0
    out = capsys.readouterr().out
    assert "✓ Cursor" in out and "Не удалось подключить: Claude Desktop" in out
    assert "mirea" in json.loads(cursor.read_text(encoding="utf-8"))["mcpServers"]


def test_choosing_only_prompt_is_success(env):
    assert installer.install(True, mode="prompt") == 0


def test_antigravity_merge_json(tmp_path, monkeypatch):
    agy_cfg = tmp_path / "gemini" / "config" / "mcp_config.json"
    agy_cfg.parent.mkdir(parents=True)
    agy_cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(installer, "antigravity_config", lambda: agy_cfg)
    monkeypatch.setattr(installer, "claude_desktop_config", lambda: tmp_path / "no-claude" / "claude.json")
    monkeypatch.setattr(installer, "cursor_config", lambda: tmp_path / "no-cursor" / "mcp.json")
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    for var in ("CODEX_HOME", "HERMES_HOME", "OPENCLAW_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var))
    monkeypatch.setenv("PYMIREA_HOME", str(tmp_path / "pm"))
    assert installer.install(True, mode="all") == 0
    data = json.loads(agy_cfg.read_text(encoding="utf-8"))
    assert "mirea" in data["mcpServers"]
    assert installer.install(False) == 0
    data_after = json.loads(agy_cfg.read_text(encoding="utf-8"))
    assert "mirea" not in data_after.get("mcpServers", {})
