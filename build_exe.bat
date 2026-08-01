@echo off
chcp 65001 >NUL
setlocal enabledelayedexpansion

REM ============================================================
REM Bilibili Downloader - exe build with bundled ffmpeg/ffprobe
REM Output: dist\BilibiliDownloader.exe (single file, self-contained)
REM ============================================================

pushd "%~dp0"

where python >NUL 2>NUL
if errorlevel 1 (
    echo [ERROR] Python is not installed.
    pause
    exit /b 1
)

REM ---- Locate ffmpeg.exe and ffprobe.exe ----
set FFMPEG_EXE=
set FFPROBE_EXE=
for /f "delims=" %%i in ('where ffmpeg 2^>NUL') do if not defined FFMPEG_EXE set "FFMPEG_EXE=%%i"
for /f "delims=" %%i in ('where ffprobe 2^>NUL') do if not defined FFPROBE_EXE set "FFPROBE_EXE=%%i"

if not defined FFMPEG_EXE (
    echo [ERROR] ffmpeg.exe not found in PATH.
    echo Install: winget install Gyan.FFmpeg
    pause
    exit /b 1
)
if not defined FFPROBE_EXE (
    echo [ERROR] ffprobe.exe not found in PATH.
    pause
    exit /b 1
)

echo Using ffmpeg:  !FFMPEG_EXE!
echo Using ffprobe: !FFPROBE_EXE!
echo.

echo [1/4] Installing build dependencies...
python -m pip install --quiet --upgrade pyinstaller yt-dlp
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo [2/4] Cleaning previous build...
if exist build rmdir /S /Q build
if exist dist rmdir /S /Q dist
if exist BilibiliDownloader.spec del /Q BilibiliDownloader.spec

echo [3/4] Building exe with bundled ffmpeg/ffprobe...
python -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "BilibiliDownloader" ^
    --collect-all yt_dlp ^
    --add-binary "!FFMPEG_EXE!;." ^
    --add-binary "!FFPROBE_EXE!;." ^
    main.py
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    pause
    exit /b 1
)

echo [4/4] Creating release zip...
set RELEASE_DIR=release
if exist %RELEASE_DIR% rmdir /S /Q %RELEASE_DIR%
mkdir %RELEASE_DIR%
copy /Y "dist\BilibiliDownloader.exe" "%RELEASE_DIR%\BilibiliDownloader.exe" >NUL
powershell -NoProfile -Command "Compress-Archive -Path '%RELEASE_DIR%\*' -DestinationPath '%RELEASE_DIR%\BilibiliDownloader.zip' -Force"

echo.
echo ============================================================
echo  Build complete.
echo  Single file:  %~dp0dist\BilibiliDownloader.exe
echo  Release zip:  %~dp0%RELEASE_DIR%\BilibiliDownloader.zip
echo ============================================================
echo.

popd
pause
