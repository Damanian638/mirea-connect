"""Установщики install.sh (запускается с подменёнными uv/curl) и install.ps1 (статические проверки)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SH = ROOT / "install.sh"
PS1 = ROOT / "install.ps1"

needs_sh = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"), reason="нужен POSIX sh")


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path: Path, *, uv: str | None = "ok", connector_rc: int = 0, curl_ok: bool = True):
    """Запустить install.sh без сети. Каталог программ — с пробелом и кириллицей."""
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    bindir = tmp_path / "Иван Петров" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "calls.log"
    _exe(bindir / "mirea-connect", f'echo "connector $*" >> "{log}"\nexit {connector_rc}\n')
    uv_body = (
        f'echo "uv $*" >> "{log}"\n'
        f'if [ "$1 $2" = "tool dir" ]; then echo "{bindir}"; exit 0; fi\n'
        f'if [ "$1 $2" = "tool install" ]; then {"echo boom >&2; exit 2" if uv == "fail" else "exit 0"}; fi\n'
    )
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    if uv is not None:
        _exe(fake / "uv", uv_body)
    else:
        # uv нет: «официальный установщик» uv кладёт его в ~/.local/bin, если curl сработал
        script = f'mkdir -p "$HOME/.local/bin"; cat > "$HOME/.local/bin/uv" <<"EOF"\n#!/bin/sh\n{uv_body}EOF\nchmod +x "$HOME/.local/bin/uv"\n'
        _exe(fake / "curl", f'echo "curl $*" >> "{log}"\n' + (f"cat <<'SCRIPT'\n{script}SCRIPT\n" if curl_ok else "exit 6\n"))
    path = os.pathsep.join([str(fake), "/usr/bin", "/bin"])
    env = {"PATH": path, "HOME": str(home), "LANG": "C.UTF-8"}
    r = subprocess.run(["sh", str(SH)], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60)
    calls = log.read_text(encoding="utf-8") if log.exists() else ""
    return r, calls


@needs_sh
def test_sh_success(tmp_path):
    r, calls = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Готово" in r.stdout
    assert "--reinstall" in calls  # обновление не берёт устаревшую копию из кеша uv
    assert "connector install --all" in calls  # без терминала — ко всем найденным клиентам


@needs_sh
def test_sh_rerun_is_safe(tmp_path):
    assert _run(tmp_path)[0].returncode == 0
    r, calls = _run(tmp_path)
    assert r.returncode == 0 and calls.count("connector install") == 2


@needs_sh
def test_sh_uv_install_failure_stops_before_registration(tmp_path):
    r, calls = _run(tmp_path, uv="fail")
    assert r.returncode != 0
    assert "Готово" not in r.stdout
    assert "connector" not in calls
    assert "Не удалось установить MIREA Connect" in r.stderr


@needs_sh
def test_sh_connector_failure_is_not_reported_as_success(tmp_path):
    r, _ = _run(tmp_path, connector_rc=1)
    assert r.returncode != 0
    assert "Готово" not in r.stdout
    assert "не удалось подключить" in r.stderr.lower()


@needs_sh
def test_sh_installs_uv_when_missing(tmp_path):
    r, calls = _run(tmp_path, uv=None)
    assert r.returncode == 0, r.stderr
    assert "curl" in calls and "uv tool install" in calls


@needs_sh
def test_sh_uv_download_failure(tmp_path):
    r, calls = _run(tmp_path, uv=None, curl_ok=False)
    assert r.returncode != 0
    assert "Готово" not in r.stdout
    assert "Не удалось установить uv" in r.stderr
    assert "connector" not in calls


# --- install.ps1: проверить Windows здесь нельзя, поэтому фиксируем обязательные защиты ---

PS = PS1.read_text(encoding="utf-8")


def _code_lines() -> list[str]:
    return [ln for ln in PS.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_ps1_checks_exit_code_of_every_native_step():
    """$ErrorActionPreference='Stop' не останавливает скрипт при ошибке uv/python — нужен $LASTEXITCODE."""
    code = "\n".join(_code_lines())
    for step in ("uv tool install", "tool dir --bin", "install"):
        assert step in code
    # после каждого внешнего вызова — проверка кода возврата
    call = re.compile(r'^\s*(\$\w+\s*=\s*)?(uv\s|powershell\s|&\s*"?\$Bin)')
    native = [i for i, ln in enumerate(_code_lines()) if call.search(ln)]
    assert len(native) >= 4, "не найдены вызовы uv/коннектора"
    for i in native:
        tail = "\n".join(_code_lines()[i : i + 4])
        assert "$LASTEXITCODE" in tail or "$?" in tail, f"нет проверки после: {_code_lines()[i]}"


def test_ps1_success_message_is_last_and_failures_throw():
    lines = _code_lines()
    done = [i for i, ln in enumerate(lines) if "Готово" in ln]
    assert len(done) == 1 and done[0] >= len(lines) - 3
    assert "throw" in PS  # ненулевой код при powershell -c, окно пользователя при irm|iex не закрывается
    assert not re.search(r"^\s*exit\b", PS, re.M)


def test_ps1_handles_cyrillic_paths_and_busy_files():
    assert "[Console]::OutputEncoding" in PS and "UTF8" in PS  # путь из `uv tool dir` с кириллицей
    assert "--reinstall" in PS
    assert "Закройте" in PS  # подсказка, если файлы заняты запущенным клиентом
    assert '& "$Bin"' in PS or "& $Bin" in PS
