@echo off
title iSync
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [iSync] Ilk kurulum yapiliyor, bir kez surer...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist ".venv\Scripts\python.exe" (
    echo Python bulunamadi. https://www.python.org/downloads/ adresinden Python 3.10+ kurun.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q
)
".venv\Scripts\python.exe" -m isync %*
if errorlevel 1 pause
