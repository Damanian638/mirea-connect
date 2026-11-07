#!/bin/sh
# MIREA Connect — установка и обновление одной командой (Linux, macOS):
#   curl -LsSf https://raw.githubusercontent.com/Damanian638/mirea-connect/main/install.sh | sh
set -eu

SRC="https://github.com/Damanian638/mirea-connect/archive/refs/heads/main.zip"

fail() {
    echo >&2
    echo "Ошибка: $1" >&2
    echo "Установка не завершена. Если ошибка повторяется, сообщите о ней:" >&2
    echo "  https://github.com/Damanian638/mirea-connect/issues" >&2
    exit 1
}

if ! command -v uv >/dev/null 2>&1; then
    echo "→ Ставлю uv (менеджер Python от Astral)…"
    # Сначала скачиваем целиком: при `curl … | sh` ошибка загрузки не была бы замечена.
    UV_SCRIPT="$(curl -LsSf https://astral.sh/uv/install.sh)" \
        || fail "Не удалось установить uv: не скачался установщик. Проверьте подключение к интернету."
    printf '%s\n' "$UV_SCRIPT" | sh || fail "Не удалось установить uv."
    PATH="$HOME/.local/bin:$PATH"
    export PATH
    command -v uv >/dev/null 2>&1 || fail "Не удалось установить uv: команда uv не найдена после установки."
fi

echo "→ Ставлю mirea-connect…"
# --reinstall: при повторном запуске заново скачать main, а не взять старую копию из кеша.
uv tool install --force --reinstall --python 3.12 "$SRC" \
    || fail "Не удалось установить MIREA Connect (см. сообщения uv выше)."

BIN_DIR="$(uv tool dir --bin)" || fail "Не удалось определить каталог программ uv."
BIN="$BIN_DIR/mirea-connect"
[ -x "$BIN" ] || fail "Не найдена установленная команда $BIN."

# При `curl … | sh` stdin занят скриптом — меню читает ответы с терминала.
if ( : < /dev/tty ) 2>/dev/null; then
    "$BIN" install < /dev/tty || fail "MIREA Connect установлен, но не удалось подключить его к приложению (см. выше)."
else
    "$BIN" install --all || fail "MIREA Connect установлен, но не удалось подключить его к приложению (см. выше)."
fi

echo
echo "Готово. Команда: $BIN"
