@echo off
:: Check for admin rights
net session >nul 2>&1
if %errorLevel% == 0 (
    echo Administrative privileges confirmed.
    echo Running electron-builder...
    cd /d "%~dp0"
    call npx electron-builder --win
    echo.
    echo Build complete! You can close this window.
    pause
) else (
    echo Requesting administrative privileges...
    powershell -Command "Start-Process cmd -ArgumentList '/c cd /d %~dp0 && build.bat' -Verb RunAs"
)
