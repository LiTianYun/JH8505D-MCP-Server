@echo off
rem ============================================
rem  Programmable Battery Console launcher
rem  Starts backend automatically and opens GUI
rem ============================================
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 (
    echo [warn] pythonw not found, fallback to python console.
    python battery_gui.py
    pause
    exit /b 0
)

start "Battery Console" pythonw battery_gui.py
exit /b 0