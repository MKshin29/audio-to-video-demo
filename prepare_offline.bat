@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
set PIP_DISABLE_PIP_VERSION_CHECK=1

echo ================================================================
echo  Подготовка офлайн-комплекта (запускать на компьютере С интернетом)
echo  Скачивает библиотеки в папку wheels и модель в папку models.
echo  После этого всю папку можно скопировать на рабочее место без
echo  интернета: start.bat установит всё из локальных файлов.
echo  ВАЖНО: версия Python на обоих компьютерах должна совпадать.
echo ================================================================
echo.

if not exist "venv\Scripts\python.exe" (
    echo Сначала один раз запустите start.bat на этом компьютере, затем закройте его окно
    echo и запустите prepare_offline.bat снова.
    pause
    exit /b 1
)

venv\Scripts\python.exe -c "import sys; print('Python', sys.version.split()[0])"
echo.
echo [1/2] Скачивание библиотек в папку wheels...
venv\Scripts\python.exe -m pip download -r requirements.txt -d wheels
if errorlevel 1 (
    echo [ОШИБКА] Не удалось скачать библиотеки.
    pause
    exit /b 1
)

echo.
set "MODEL=small"
set /p "MODEL=[2/2] Какую модель скачать? small / medium / large-v3-turbo / large-v3 (Enter = small): "
venv\Scripts\python.exe -c "import sys; from faster_whisper.utils import download_model; print(download_model(sys.argv[1], cache_dir='models'))" %MODEL%
if errorlevel 1 (
    echo [ОШИБКА] Не удалось скачать модель.
    pause
    exit /b 1
)
echo.
echo Готово. Скопируйте папку целиком ^(без папок venv и workspace^) на рабочее место
echo и запустите там start.bat.
pause
