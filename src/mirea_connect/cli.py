"""Точка входа ``mirea-connect``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from . import __version__
from . import services as S


def _force_utf8() -> None:
    """Кириллица в консоли: без этого старая консоль Windows (cp866/cp1251) выдаёт
    кракозябры, а при stdout=ascii `print` вообще падает. На 3.7+ переводим потоки в UTF-8."""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _store():
    from pymirea.store import SessionStore

    store = SessionStore()
    store.configure()
    return store


async def _login_terminal(login: str | None) -> int:
    from .auth import MireaAuth

    store = _store()
    login = login or os.environ.get("MIREA_LOGIN") or input("Логин (…@edu.mirea.ru): ").strip()
    password = getpass.getpass("Пароль (не отображается): ")
    auth = MireaAuth()
    try:
        result = await auth.login(login, password)
        while result.challenge:
            code = input("Код из письма: ").strip()
            result = await auth.complete_2fa(result.challenge, code)
            if result.challenge:
                print(result.message or "Неверный код, попробуйте ещё раз.")
    finally:
        await auth.close()
    if not result.success or not result.cookies:
        print(f"Не удалось войти: {result.message}", file=sys.stderr)
        return 1
    store.save(result.cookies, login=login)
    print("Вы вошли. Сессия сохранена на этом компьютере.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mirea-connect", description="MCP-коннектор к РТУ МИРЭА")
    p.add_argument("--version", action="version", version=f"mirea-connect {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="КОМАНДА")

    s = sub.add_parser("install", help="подключить коннектор к клиентам (меню выбора)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="без вопросов, ко всем найденным клиентам")
    g.add_argument("--prompt", action="store_true", help="только показать промпт для другого агента")
    sub.add_parser("uninstall", help="отключить коннектор от клиентов")
    s = sub.add_parser("prompt", help="промпт для любого другого агента, чтобы он подключил коннектор сам")
    s.add_argument("--json", action="store_true", help="только параметры сервера в JSON")
    s = sub.add_parser("login", help="войти в МИРЭА в терминале")
    s.add_argument("login", nargs="?")
    sub.add_parser("logout", help="удалить сессию с этого компьютера")
    sub.add_parser("status", help="есть ли вход")
    sub.add_parser("local", help="локальный MCP-сервер (stdio) — его запускает клиент")
    s = sub.add_parser("serve", help="удалённый сервер с OAuth (для self-host)")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--public-url")

    from .commands import DATA_COMMANDS, add_subparsers

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="вывести результат в JSON")
    add_subparsers(sub, common)

    args = p.parse_args(argv)
    if args.cmd != "local":  # у local stdout — канал MCP-протокола, перекодировать нельзя
        _force_utf8()
    if args.cmd in DATA_COMMANDS:
        from .commands import run as run_data

        try:
            return asyncio.run(run_data(args, _store()))
        except S.ServiceError as e:
            print(f"Ошибка: {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    try:
        if args.cmd == "install":
            from .installer import install

            return install(True, mode="all" if args.all else "prompt" if args.prompt else "auto")
        if args.cmd == "prompt":
            import json

            from .installer import agent_prompt, server_spec

            print(json.dumps(server_spec(), ensure_ascii=False) if args.json else agent_prompt())
            return 0
        if args.cmd == "uninstall":
            from .installer import install

            return install(False)
        if args.cmd == "login":
            return asyncio.run(_login_terminal(args.login))
        if args.cmd == "logout":
            print("Сессия удалена." if _store().clear() else "Входа и так не было.")
            return 0
        if args.cmd == "status":
            from pymirea.store import NotLoggedIn

            try:
                s = _store().load()
                print(f"Вход выполнен: {s.login}")
                return 0
            except NotLoggedIn:
                print("Вход не выполнен. `mirea-connect login`")
                return 1
        if args.cmd == "local":
            from .local import run_stdio

            run_stdio()
            return 0
        if args.cmd == "serve":
            from .server import serve

            serve(args.host, args.port, args.public_url)
            return 0
    except KeyboardInterrupt:
        return 130
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
