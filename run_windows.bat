@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (set "PY=py -3") else (set "PY=python")
if not exist .venv ( %PY% -m venv .venv || goto :fail )
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || goto :fail
python -m streamlit run research_app.py
exit /b %ERRORLEVEL%
:fail
echo Setup failed. Ensure Python 3.10 or newer is installed, then rerun this file.
pause
exit /b 1
