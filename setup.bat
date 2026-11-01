@echo off
rem JARVIS one-time setup (Windows 10/11). Run it from the JARVIS folder, or
rem double-click it. Steps: find Python 3.11, make the venv, install the
rem pinned packages, download the voice/camera models, create .env.
rem Needs internet this one time only. "setup.bat /quiet" skips the pause.
setlocal
cd /d "%~dp0"
echo.
echo === JARVIS setup ===

rem -- 1. Python 3.11 (the py launcher first, then python on PATH) --------
set "PY="
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.11"
if defined PY goto have_python
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto have_python
echo.
echo Python 3.11 was not found on this computer.
echo JARVIS needs Python 3.11, 64-bit. Download it from
echo   https://www.python.org/downloads/release/python-3117/
echo During install, tick "Add python.exe to PATH". Then run setup.bat again.
goto fail

:have_python
for /f "delims=" %%v in ('%PY% -c "import sys; print(sys.version.split()[0])"') do echo [1/5] Python %%v found (%PY%).

rem -- 2. The virtual environment ------------------------------------------
if exist "venv\Scripts\python.exe" (
    echo [2/5] venv already exists - kept.
) else (
    echo [2/5] Creating the virtual environment in venv\ ...
    %PY% -m venv venv
    if errorlevel 1 goto venv_failed
)

rem -- 3. Packages (pinned versions) ----------------------------------------
echo [3/5] Installing packages from requirements.txt (a few minutes the first time) ...
"venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto pip_failed

rem -- 4. Models: wake word, Whisper base.en, Kokoro voice, YOLOX -----------
echo [4/5] Downloading the models into models\ (about 520 MB, kept if already there) ...
"venv\Scripts\python.exe" scripts\download_models.py
if errorlevel 1 goto models_failed

rem -- 5. .env (secrets; Groq key optional) ----------------------------------
if exist ".env" (
    echo [5/5] .env already exists - kept.
) else (
    copy /y ".env.example" ".env" >nul
    echo [5/5] Created .env from .env.example. Add your free GROQ_API_KEY there for online answers.
)

echo.
echo Setup finished. Start JARVIS with run.bat
set "RESULT=0"
goto end

:venv_failed
echo Could not create the virtual environment. Check that Python 3.11 is installed correctly.
goto fail
:pip_failed
echo Installing the packages failed. Check the internet connection, then run setup.bat again.
goto fail
:models_failed
echo Downloading the models failed. Check the internet connection, then run setup.bat again
echo (files already downloaded are kept).
goto fail

:fail
set "RESULT=1"
:end
if /i not "%~1"=="/quiet" pause
exit /b %RESULT%
