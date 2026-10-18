@echo off
setlocal EnableDelayedExpansion
title Motive Safety Console
cd /d "%~dp0"

echo ========================================================
echo   Starting Motive Safety Monitoring Console
echo   Organizations: HEART (260129) ^& Houston Towing (599530)
echo ========================================================

:: 1. Check if virtual environment already exists and works
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if !errorlevel! equ 0 goto :check_deps
    echo [!] Existing .venv appears broken or from another machine. Recreating...
    rmdir /s /q ".venv" 2>nul
)

:: 2. Locate a working Python 3 interpreter on Windows
set "SYS_PYTHON="

:: Try Windows Python Launcher (py -3) first - works even if PATH wasn't checked during install
py -3 -c "import sys" >nul 2>&1
if !errorlevel! equ 0 (
    set "SYS_PYTHON=py -3"
    goto :create_venv
)

:: Try 'python' on PATH (verifying it's a real interpreter and not the Windows Store stub)
python -c "import sys" >nul 2>&1
if !errorlevel! equ 0 (
    set "SYS_PYTHON=python"
    goto :create_venv
)

:: Try 'python3' on PATH
python3 -c "import sys" >nul 2>&1
if !errorlevel! equ 0 (
    set "SYS_PYTHON=python3"
    goto :create_venv
)

:: Search standard Windows installation directories
call :find_python_path
if defined SYS_PYTHON goto :create_venv

:: 3. If Python is not installed at all, attempt automatic install via winget
echo.
echo [!] Python 3 was not found on this computer.
where winget >nul 2>&1
if !errorlevel! equ 0 (
    echo [*] Installing Python 3.12 automatically via Windows Package Manager ^(winget^)...
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    
    :: Re-check py launcher and standard paths after winget install
    py -3 -c "import sys" >nul 2>&1
    if !errorlevel! equ 0 (
        set "SYS_PYTHON=py -3"
        goto :create_venv
    )
    call :find_python_path
    if defined SYS_PYTHON goto :create_venv
)

echo.
echo [ERROR] Could not find or install Python 3 automatically.
echo Please download and install Python 3 from: https://www.python.org/downloads/
echo ^(Important: Check "Add python.exe to PATH" at the bottom of the installer^)
start https://www.python.org/downloads/
pause
exit /b 1

:create_venv
echo [*] First-time setup: Creating virtual environment ^(.venv^) using !SYS_PYTHON!...
!SYS_PYTHON! -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
)

:check_deps
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, websockets" >nul 2>&1
if !errorlevel! neq 0 (
    echo [*] Installing required packages from requirements.txt...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to install dependencies. Please check your internet connection.
        pause
        exit /b 1
    )
)

:: 4. Run the console
".venv\Scripts\python.exe" run.py %*
pause
exit /b 0

:find_python_path
for %%V in (314 313 312 311 310 39) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set "SYS_PYTHON="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe""
        exit /b 0
    )
    if exist "%ProgramFiles%\Python%%V\python.exe" (
        set "SYS_PYTHON="%ProgramFiles%\Python%%V\python.exe""
        exit /b 0
    )
    if exist "C:\Python%%V\python.exe" (
        set "SYS_PYTHON="C:\Python%%V\python.exe""
        exit /b 0
    )
)
exit /b 1
