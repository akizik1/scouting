@echo off
REM Double-click to start Scout Finder (Windows)
cd /d "%~dp0"
title Scout Finder
chcp 65001 >nul
echo.
echo   Scout Finder
echo.
set "PY="
where py >nul 2>&1 && (py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PY=py -3")
if not defined PY (
  where python >nul 2>&1 && (python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PY=python")
)
if not defined PY (
  echo Python 3.10+ is needed. Opening the download page...
  echo During install, tick "Add python.exe to PATH". Then double-click this file again.
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo First start: setting up, about 1 minute...
  %PY% -m venv .venv || (echo Could not create the environment. & pause & exit /b 1)
)
if not exist ".venv\.installed" (
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (echo Install failed. Check your internet connection. & pause & exit /b 1)
  echo ok> ".venv\.installed"
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
