#!/usr/bin/env python3
"""
Локальный сервер для веб-интерфейса расписания (tools/timetable_grid.html).
Использует MireaSchedule из библиотеки pymirea для поиска и получения расписания без CORS.

Запуск:
    python tools/server.py
"""

from __future__ import annotations

import asyncio
import http.server
import json
import socketserver
import sys
import traceback
import webbrowser
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent

# Добавление пути к src/ проекта для поиска pymirea
for base_dir in (TOOLS_DIR, TOOLS_DIR.parent):
    candidate_src = base_dir / "src"
    if candidate_src.exists() and str(candidate_src) not in sys.path:
        sys.path.insert(0, str(candidate_src))
        break

def _ensure_runtime_dependencies():
    missing = []
    try:
        import pymirea  # noqa: F401 - import verifies the installed runtime
    except ImportError:
        missing.append("pymirea")
    try:
        import openpyxl  # noqa: F401 - import verifies the installed runtime
    except ImportError:
        missing.append("openpyxl")

    if missing:
        print(f"[*] Отсутствуют библиотеки: {', '.join(missing)}. Запускаю автоустановку...")
        import subprocess
        repo_root = TOOLS_DIR.parent
        cmd = [sys.executable, "-m", "pip", "install", "openpyxl", "-e", str(repo_root)]
        try:
            subprocess.check_call(cmd)
            print("[✓] Библиотеки успешно установлены.")
        except Exception:
            subprocess.check_call([
                sys.executable, "-m", "pip", "install", "openpyxl",
                "pymirea @ https://github.com/atomic-pancake-49/pymirea/archive/b4af5fcfbc6d34bc1986ae8cec2298d95aa7901e.zip"
            ])

_ensure_runtime_dependencies()

from pymirea.schedule import MireaSchedule  # noqa: E402 - dependencies are checked first

try:
    from pymirea.schedule import SCHEDULE_ORGANIZATION_ID
except ImportError:
    SCHEDULE_ORGANIZATION_ID = "mirea"


async def _search_targets(target_type: str, query: str) -> list[dict]:
    sched = MireaSchedule()
    try:
        if target_type == "classroom":
            raw = await sched.search_classrooms(query)
        elif target_type == "teacher":
            raw = await sched.search_teachers(query)
        elif target_type == "group":
            raw = await sched.search_groups(query)
        else:
            raw = []

        results = []
        for item in raw:
            uid = getattr(item, "uid", None) or getattr(item, "external_id", None) or getattr(item, "id", None)
            name = getattr(item, "name", "") or getattr(item, "full_title", "") or getattr(item, "target_title", "")
            results.append({
                "uid": str(uid) if uid is not None else "",
                "name": str(name),
            })
        return results
    finally:
        await sched.close()


async def _get_schedule(target_type: str, uid: str) -> tuple[list | dict | None, str | None]:
    sched = MireaSchedule()
    try:
        payload, err = await sched._rpc(
            "get_schedule_for_entity",
            {
                "p_entity_type": target_type,
                "p_entity": str(uid),
                "p_organization_id": SCHEDULE_ORGANIZATION_ID,
            },
        )
        return payload, err
    finally:
        await sched.close()


class TimetableProxyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(TOOLS_DIR), **kwargs)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/timetable_grid.html")
            self.end_headers()
            return
        return super().do_GET()

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len) if content_len > 0 else b"{}"

        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": "Некорректный JSON"})
            return

        # 1. Поиск сущности (аудитории, преподавателя, группы)
        if self.path == "/api/search":
            t_type = body.get("type", "classroom")
            query = body.get("query", "")
            try:
                results = asyncio.run(_search_targets(t_type, query))
                self._send_json(200, results)
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                self._send_json(500, {"error": f"Ошибка поиска: {type(e).__name__} {e}".strip()})
            return

        # 2. Получение расписания по uid
        if self.path == "/api/schedule":
            t_type = body.get("type", "classroom")
            uid = body.get("uid", "")
            try:
                payload, err = asyncio.run(_get_schedule(t_type, uid))
                if err:
                    self._send_json(502, {"error": f"API МИРЭА вернул ошибку: {err}"})
                else:
                    self._send_json(200, payload if payload is not None else [])
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                self._send_json(500, {"error": f"Ошибка API расписания: {type(e).__name__} {e}".strip()})
            return

        self.send_error(404, "Endpoint not found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _send_json(self, status: int, data):
        resp = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def run_server(port: int = 8000):
    for p in range(port, port + 10):
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", p), TimetableProxyHandler)
            url = f"http://127.0.0.1:{p}/timetable_grid.html"
            print("=" * 65)
            print(" [✓] Локальный сервер расписания РТУ МИРЭА запущен!")
            print(f" -> Адрес: {url}")
            print(" -> Бэкенд: pymirea.schedule (CORS исключён)")
            print(" Для остановки нажмите Ctrl+C")
            print("=" * 65)
            webbrowser.open(url)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nСервер остановлен.")
            return
        except OSError:
            continue

    print(f"Ошибка: не удалось занять порты от {port} до {port+9}.", file=sys.stderr)


if __name__ == "__main__":
    p = 8000
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        p = int(sys.argv[1])
    run_server(p)
