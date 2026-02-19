@echo off
setlocal enabledelayedexpansion
REM Always run from this folder
cd /d "%~dp0"
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
echo Installing requirements...
py -m pip install --upgrade pip -q
py -m pip install -r requirements.txt -q
if errorlevel 1 (
  echo.
  echo ERROR: Failed to install requirements.
  pause
  exit /b 1
)

echo.
echo Checking for updates...
py updater.py

echo.
echo Validating license...
py -c "from license import validate_license; validate_license()"
if errorlevel 1 (
  echo.
  echo License validation failed.
  pause
  exit /b 1
)

echo.
echo Running PACbot...
py main.py
echo.
echo PACbot exited.
pause
