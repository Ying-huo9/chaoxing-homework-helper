@echo off
REM Manual cleanup for the packaging/ folder that PyInstaller left behind.
REM One file inside (base_library.zip) is held by an antivirus / search
REM indexer process; the only way to release it is to wait for the scanner
REM to finish or to boot the machine once.
REM
REM Right-click this file and choose "Run as administrator".

setlocal
set "TARGET=%~dp0packaging"

if not exist "%TARGET%" (
    echo packaging\ does not exist, nothing to do.
    pause
    exit /b 0
)

echo Deleting: %TARGET%
del /f /s /q "%TARGET%" 2>nul
rd /s /q "%TARGET%" 2>nul

if exist "%TARGET%" (
    echo.
    echo Could not delete packaging\ completely. A file inside is still locked.
    echo Try this in order:
    echo   1. Pause / disable Windows Search Indexer and your antivirus briefly.
    echo   2. Reboot the computer, then run this script again before any
    echo      antivirus scan starts.
    echo   3. As a last resort, use Sysinternals handle.exe to find the lock.
) else (
    echo Done.
)
pause
endlocal
