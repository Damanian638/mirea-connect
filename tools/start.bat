@echo off
chcp 65001 >nul
title MIREA Timetable Tool
cd /d "%~dp0"

echo ===================================================
echo   MIREA Timetable Tool - Запуск
echo ===================================================
echo.

set "PY_CMD="

:: 1. Проверяем локальный .venv
if exist ".venv\Scripts\python.exe" (
    set "PY_CMD=.venv\Scripts\python.exe"
    goto :found_python
)
if exist "..\.venv\Scripts\python.exe" (
    set "PY_CMD=..\.venv\Scripts\python.exe"
    goto :found_python
)

:: 2. Проверяем системный лаунчер py
py -3 -c "import sys" >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=py -3"
    goto :found_python
)

:: 3. Проверяем python
python -c "import sys" >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=python"
    goto :found_python
)

:: 4. Проверяем uv
uv --version >nul 2>nul
if %errorlevel% equ 0 (
    set "PY_CMD=uv run python"
    goto :found_python
)

echo [!] Ошибка: Python не найден в системе.
echo Пожалуйста, установите Python с сайта https://www.python.org/
echo При установке обязательно отметьте галочку: Add python.exe to PATH
goto :finish

:found_python
echo [✓] Интерпретатор: %PY_CMD%

:: Поиск стартового скрипта
set "TARGET="
if exist "start.py" set "TARGET=start.py"
if not defined TARGET if exist "tools\server.py" set "TARGET=tools\server.py"
if not defined TARGET if exist "server.py" set "TARGET=server.py"

if not defined TARGET (
    echo [!] Ошибка: не найден файл start.py или server.py!
    goto :finish
)

echo [✓] Файл запуска: %TARGET%
echo [*] Запуск приложения...
echo ---------------------------------------------------
echo.

%PY_CMD% "%TARGET%"

echo.
echo ---------------------------------------------------
echo [*] Работа программы завершена (код: %errorlevel%).

:finish
echo.
echo Для закрытия окна нажмите любую клавишу...
pause >nul