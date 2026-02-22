@echo off
setlocal enabledelayedexpansion
REM Always run from this folder
cd /d "%~dp0"
REM UTF-8 encoding for Python (prevents Unicode crashes on Windows console)
set PYTHONIOENCODING=utf-8
echo ============================
echo PACbot - Setup + Run
echo ============================
REM Check Python
py -V >nul 2>&1
if errorlevel 1 (
  echo.
  echo ERROR: Python Launcher "py" not found.
  echo Install Python from https://python.org and make sure "Python Launcher" is checked.
  pause
  exit /b 1
)
REM Create venv if missing
if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Creating virtual environment...
  py -m venv .venv
  if errorlevel 1 (
    echo ERROR: Failed to create venv.
    pause
    exit /b 1
  )
)
echo.
echo Activating venv...
call ".venv\Scripts\activate.bat"
echo.
py -m pip install --upgrade pip -qq 2>nul

REM Check if first-time setup (easyocr not installed yet)
set FIRST_RUN=0
py -c "import easyocr" >nul 2>&1
if errorlevel 1 set FIRST_RUN=1

if %FIRST_RUN%==1 (
  echo First-time setup: downloading OCR engine.
  echo This only happens once and may take a few minutes.
  echo.
  py -m pip install -r requirements.txt 2>nul
) else (
  echo Installing requirements...
  py -m pip install -r requirements.txt -qq 2>nul
)
if errorlevel 1 (
  echo.
  echo ERROR: Failed to install requirements.
  pause
  exit /b 1
)
echo Done!

echo.
py updater.py

echo.
py main.py
echo.
echo PACbot exited.
