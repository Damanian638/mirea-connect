# MIREA Connect — установка одной командой (Windows, PowerShell):
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Damanian638/mirea-connect/main/install.ps1 | iex"
$ErrorActionPreference = "Stop"
$Src = "https://github.com/Damanian638/mirea-connect/archive/refs/heads/main.zip"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "-> Ставлю uv (менеджер Python от Astral)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "-> Ставлю mirea-connect..."
uv tool install --force --python 3.12 $Src

$Bin = Join-Path (uv tool dir --bin) "mirea-connect.exe"
& $Bin install

Write-Host ""
Write-Host "Готово. Команда: $Bin"
