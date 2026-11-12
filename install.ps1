# MIREA Connect — установка одной командой (Windows, PowerShell):
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Damanian638/mirea-connect/main/install.ps1 | iex"
# Отдельная область видимости нужна и при запуске через irm | iex.
& {
    $ErrorActionPreference = "Stop"
    $MireaSource = "https://github.com/Damanian638/mirea-connect/archive/refs/heads/main.zip"

    $PrevOutputEncoding = [Console]::OutputEncoding
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "-> Ставлю uv (менеджер Python от Astral)..."
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
        if ($LASTEXITCODE -ne 0) {
            throw "Не удалось установить uv. Установка MIREA Connect остановлена."
        }
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw "Команда uv не найдена. Откройте новое окно PowerShell и повторите установку."
        }
    }

    $MireaToolsDirectory = uv tool dir
    if ($LASTEXITCODE -ne 0 -or -not $MireaToolsDirectory) {
        throw "Не удалось определить папку инструментов uv. Установка остановлена."
    }
    $MireaEnvironment = [IO.Path]::GetFullPath((Join-Path $MireaToolsDirectory "mirea-connect"))
    $MireaEnvironmentPrefix = $MireaEnvironment.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    $MireaProcesses = @()
    try {
        # Не выводим командные строки процессов: в них могут быть личные данные.
        $MireaProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'mirea-connect.exe'" -ErrorAction Stop |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($MireaEnvironmentPrefix, [StringComparison]::OrdinalIgnoreCase) })
    }
    catch {
        Write-Host "Не удалось проверить запущенный коннектор. Перед обновлением отключите сервер mirea в своих ИИ-приложениях."
    }
    if ($MireaProcesses.Count -gt 0) {
        throw "MIREA Connect сейчас запущен. Отключите сервер mirea в настройках MCP или полностью закройте использующие его приложения и повторите эту команду. После установки включите сервер снова."
    }

    Write-Host "-> Ставлю mirea-connect..."
    uv tool install --force --reinstall --python 3.12 $MireaSource
    if ($LASTEXITCODE -ne 0) {
        throw "Установка MIREA Connect не завершена. Если указано 'Отказано в доступе' (os error 5), отключите сервер mirea в ИИ-приложениях или Закройте их и повторите установку. Если ошибка сохраняется, проверьте блокировку антивирусом и права доступа."
    }

    $MireaBinDirectory = uv tool dir --bin
    if ($LASTEXITCODE -ne 0 -or -not $MireaBinDirectory) {
        throw "Не удалось определить путь запуска MIREA Connect. Установка остановлена."
    }
    $Bin = Join-Path $MireaBinDirectory "mirea-connect.exe"
    & "$Bin" --version
    if ($LASTEXITCODE -ne 0) {
        throw "MIREA Connect установлен, но не запускается. Подключение к ИИ-приложениям не выполнено."
    }
    & "$Bin" install
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось настроить подключение к ИИ-приложению. Повторите команду '$Bin install'."
    }

    [Console]::OutputEncoding = $PrevOutputEncoding
    Write-Host ""
    Write-Host "Готово. Команда: $Bin"
}
