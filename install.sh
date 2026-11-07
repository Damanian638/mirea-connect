#!/bin/sh
# MIREA Connect — установка одной командой (Linux, macOS):
#   curl -LsSf https://raw.githubusercontent.com/Damanian638/mirea-connect/main/install.sh | sh
set -eu

SRC="https://github.com/Damanian638/mirea-connect/archive/refs/heads/main.zip"

if ! command -v uv >/dev/null 2>&1; then
    echo "→ Ставлю uv (менеджер Python от Astral)…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
    export PATH
fi

echo "→ Ставлю mirea-connect…"
uv tool install --force --python 3.12 "$SRC"

BIN="$(uv tool dir --bin)/mirea-connect"
# При `curl … | sh` stdin занят скриптом — меню читает ответы с терминала.
if ( : < /dev/tty ) 2>/dev/null; then
    "$BIN" install < /dev/tty
else
    "$BIN" install --all
fi

echo
echo "Готово. Команда: $BIN"
