@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title Audio to video chat
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
set HF_HUB_DISABLE_TELEMETRY=1
set PIP_DISABLE_PIP_VERSION_CHECK=1

if exist "venv\Scripts\python.exe" goto deps

echo.
echo Первый запуск: создаю окружение Python в папке venv...
set "PY="
for %%V in (3.12 3.11 3.10 3.13 3.9) do (
    if not defined PY (
        py -%%V -c "pass" >nul 2>nul && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo.
    echo [ОШИБКА] Не найден Python 3.9-3.13.
    echo Установите Python с https://www.python.org/downloads/ ^(галочка "Add python.exe to PATH"^)
    echo или попросите администратора, затем запустите start.bat снова.
    pause
    exit /b 1
)
echo Использую: %PY%
%PY% -m venv venv
if errorlevel 1 (
    echo [ОШИБКА] Не удалось создать окружение Python.
    pause
    exit /b 1
)

:deps
fc /b requirements.txt venv\requirements.installed >nul 2>nul
if not errorlevel 1 goto run
echo.
if exist "wheels\" (
    echo Установка библиотек из папки wheels ^(без интернета^)...
    venv\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements.txt
) else (
    echo Установка библиотек ^(нужен интернет, только при первом запуске^)...
    venv\Scripts\python.exe -m pip install -r requirements.txt
)
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Не удалось установить библиотеки. Проверьте доступ к интернету или прокси ^(см. README^).
    pause
    exit /b 1
)
copy /y requirements.txt venv\requirements.installed >nul

:run
echo.
venv\Scripts\python.exe app\server.py %*
if errorlevel 1 pause
