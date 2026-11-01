@echo off
rem Start JARVIS (her own window, no console). Run setup.bat once first.
setlocal
cd /d "%~dp0"
if not exist "venv\Scripts\pythonw.exe" (
    echo JARVIS isn't set up yet. Run setup.bat first, then run.bat again.
    pause
    exit /b 1
)
call "venv\Scripts\activate.bat"
start "" "venv\Scripts\pythonw.exe" main.py
exit /b 0
