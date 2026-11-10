#!/usr/bin/env python3
"""
Автозапуск MIREA Timetable Tool:
Проверяет библиотеки и запускает tools/server.py.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

# Определение каталогов относительно расположения start.py
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR if (SCRIPT_DIR / "src").exists() else SCRIPT_DIR.parent
VENV_DIR = ROOT_DIR / ".venv"


def find_server_script() -> Path | None:
    candidates = [
        ROOT_DIR / "tools" / "server.py",
        SCRIPT_DIR / "tools" / "server.py",
        SCRIPT_DIR / "server.py",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def get_venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def is_ready(py_path: str | Path) -> bool:
    try:
        r = subprocess.run([str(py_path), "-c", "import pymirea, openpyxl"], capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


def setup_dependencies() -> Path | str:
    # 1. Если в текущем интерпретаторе уже есть библиотеки
    if is_ready(sys.executable):
        return sys.executable

    # 2. Если есть готовый .venv
    venv_python = get_venv_python()
    if venv_python.exists() and is_ready(venv_python):
        return venv_python

    # 3. Если .venv нет — создаем
    target_py = venv_python
    if not venv_python.exists():
        print("[*] Создание изолированного виртуального окружения (.venv)...")
        try:
            import venv
            venv.create(VENV_DIR, with_pip=True)
            print("[✓] Виртуальное окружение создано.")
        except Exception as e:
            print(f"[!] Не удалось создать .venv ({e}), использую текущий Python...")
            target_py = Path(sys.executable)

    # 4. Установка библиотек
    print("[*] Установка зависимостей (pymirea, openpyxl)...")
    subprocess.run([str(target_py), "-m", "pip", "install", "--upgrade", "pip", "-q"], capture_output=True)

    install_cmd = [str(target_py), "-m", "pip", "install", "openpyxl", "-e", str(ROOT_DIR)]
    r = subprocess.run(install_cmd)

    if r.returncode != 0:
        print("[*] Установка pymirea по прямой ссылке...")
        fallback_cmd = [
            str(target_py),
            "-m",
            "pip",
            "install",
            "openpyxl",
            "pymirea @ https://github.com/atomic-pancake-49/pymirea/archive/b4af5fcfbc6d34bc1986ae8cec2298d95aa7901e.zip",
        ]
        subprocess.run(fallback_cmd, check=True)

    print("[✓] Все библиотеки успешно установлены.")
    return target_py


def main():
    server_py = find_server_script()
    if not server_py:
        print(f"[!] Ошибка: файл server.py не найден ни в tools/, ни в корне!", file=sys.stderr)
        sys.exit(1)

    py_exe = setup_dependencies()
    print(f"[*] Запуск локального сервера: {server_py.name}")

    try:
        subprocess.run([str(py_exe), str(server_py)])
    except KeyboardInterrupt:
        print("\nСервер остановлен.")


if __name__ == "__main__":
    main()