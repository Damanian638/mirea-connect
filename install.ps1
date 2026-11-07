# MIREA Connect — установка и обновление одной командой (Windows, PowerShell):
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Damanian638/mirea-connect/main/install.ps1 | iex"
$ErrorActionPreference = "Stop"
$Src = "https://github.com/Damanian638/mirea-connect/archive/refs/heads/main.zip"
$Issues = "https://github.com/Damanian638/mirea-connect/issues"

# uv печатает пути в UTF-8; без этого путь с кириллицей (C:\Users\Иван) читается кракозябрами.
$PrevOutputEncoding = [Console]::OutputEncoding
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Fail([string]$Message) {
    [Console]::OutputEncoding = $PrevOutputEncoding
    Write-Host ""
    Write-Host "Ошибка: $Message" -ForegroundColor Red
    Write-Host "Установка не завершена. Если ошибка повторяется, сообщите о ней: $Issues"
    # throw, а не exit: при `irm | iex` в открытом окне exit закрыл бы PowerShell пользователя.
    # При запуске через `powershell -c` throw даёт ненулевой код возврата.
    throw "MIREA Connect: $Message"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "-> Ставлю uv (менеджер Python от Astral)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    if ($LASTEXITCODE -ne 0) { Fail "Не удалось установить uv. Проверьте подключение к интернету." }
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail "uv не найден после установки. Откройте новое окно PowerShell и повторите." }
}

Write-Host "-> Ставлю mirea-connect..."
# --reinstall: при повторном запуске заново скачать main, а не взять старую копию из кеша.
uv tool install --force --reinstall --python 3.12 $Src
if ($LASTEXITCODE -ne 0) {
    Fail ("Не удалось установить MIREA Connect (см. сообщения uv выше). " +
          "Если там «Отказано в доступе» / «Access is denied» или «used by another process», " +
          "значит, файлы заняты запущенным коннектором: Закройте Claude, ChatGPT / Codex, Cursor " +
          "(в том числе значок возле часов) и повторите команду.")
}

$BinDir = uv tool dir --bin
if ($LASTEXITCODE -ne 0 -or -not $BinDir) { Fail "Не удалось определить каталог программ uv." }
$Bin = Join-Path ($BinDir | Select-Object -First 1).Trim() "mirea-connect.exe"
if (-not (Test-Path -LiteralPath $Bin)) { Fail "Не найдена установленная программа: $Bin" }

[Console]::OutputEncoding = $PrevOutputEncoding
& "$Bin" install
if ($LASTEXITCODE -ne 0) { Fail "MIREA Connect установлен, но не удалось подключить его к приложению (см. выше)." }

Write-Host ""
Write-Host "Готово. Команда: $Bin"
