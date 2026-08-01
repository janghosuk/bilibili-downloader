@echo off
chcp 65001 >NUL
REM Bilibili Downloader launcher

where python >NUL 2>NUL
if errorlevel 1 (
    echo [ERROR] Python is not installed. Install from https://www.python.org/
    pause
    exit /b 1
)

echo Installing/updating yt-dlp...
python -m pip install --quiet --upgrade yt-dlp
if errorlevel 1 (
    echo [ERROR] Failed to install yt-dlp.
    pause
    exit /b 1
)

echo Starting Bilibili Downloader...
python "%~dp0main.py"
if errorlevel 1 (
    echo.
    echo [ERROR] Application exited with error.
    pause
)
