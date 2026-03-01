@echo off
setlocal enabledelayedexpansion
REM Always run from this folder
cd /d "%~dp0"
REM UTF-8 encoding for Python (prevents Unicode crashes on Windows console)
set PYTHONIOENCODING=utf-8
echo ============================
echo 9Bot - Setup + Run
echo ============================

REM Download ADB if missing (fallback for source installs without bundled binaries)
if not exist "platform-tools\adb.exe" (
  echo.
  echo Downloading ADB platform-tools...
  powershell -Command "Invoke-WebRequest -Uri 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' -OutFile '%TEMP%\platform-tools.zip'"
  if errorlevel 1 (
    echo WARNING: Failed to download platform-tools. ADB may not work.
  ) else (
    powershell -Command "Expand-Archive -Path '%TEMP%\platform-tools.zip' -DestinationPath '%TEMP%\pt-extract' -Force"
    if not exist "platform-tools" mkdir "platform-tools"
    copy /Y "%TEMP%\pt-extract\platform-tools\adb.exe" "platform-tools\" >nul
    copy /Y "%TEMP%\pt-extract\platform-tools\AdbWinApi.dll" "platform-tools\" >nul
    copy /Y "%TEMP%\pt-extract\platform-tools\AdbWinUsbApi.dll" "platform-tools\" >nul
    rd /s /q "%TEMP%\pt-extract" 2>nul
    del "%TEMP%\platform-tools.zip" 2>nul
    echo Done!
  )
)

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

REM Check if first-time setup (easyocr not installed yet)
set FIRST_RUN=0
py -c "import easyocr" >nul 2>&1
if errorlevel 1 set FIRST_RUN=1

if %FIRST_RUN%==1 (
  echo.
  echo First-time setup: downloading OCR engine.
  echo This only happens once and may take a few minutes.
  echo.
  py -m pip install --upgrade pip -qq 2>nul
  py -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo ERROR: Failed to install requirements.
    pause
    exit /b 1
  )
) else (
  REM Only install if requirements.txt changed since last install
  set NEEDS_INSTALL=0
  if not exist ".venv\.req_hash" set NEEDS_INSTALL=1
  if !NEEDS_INSTALL!==0 (
    certutil -hashfile requirements.txt MD5 2>nul | findstr /v ":" > "%TEMP%\req_hash_new.txt"
    fc /b ".venv\.req_hash" "%TEMP%\req_hash_new.txt" >nul 2>&1
    if errorlevel 1 set NEEDS_INSTALL=1
  )
  if !NEEDS_INSTALL!==1 (
    echo Installing requirements...
    py -m pip install --upgrade pip -qq 2>nul
    py -m pip install -r requirements.txt -qq
    if errorlevel 1 (
      echo.
      echo ERROR: Failed to install requirements.
      pause
      exit /b 1
    )
    certutil -hashfile requirements.txt MD5 2>nul | findstr /v ":" > ".venv\.req_hash"
  )
)
echo Done!

echo.
py updater.py

echo.
py run_web.py
if errorlevel 1 (
  echo.
  echo ==========================================
  echo 9Bot crashed! See error message above.
  echo ==========================================
  pause
  exit /b 1
)
echo.
echo 9Bot exited.
exit /b 0
