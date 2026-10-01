@echo off
chcp 65001 >nul
title ShiftAI Launcher

echo.
echo ============================================================
echo  ShiftAI - Auto Shift Generator for Nursery Schools
echo ============================================================
echo.

set PROJECT_DIR=%~dp0
cd /d "%PROJECT_DIR%"

echo [1/4] Checking Python...
python --version
if %errorlevel% neq 0 (
    echo ERROR: Python not found.
    echo Please install Python 3.11+ and add to PATH.
    echo.
    pause
    exit /b 1
)
python -c "import sys; exit(0 if sys.version_info >= (3, 11) else 1)"
if %errorlevel% neq 0 (
    echo ERROR: Python 3.11+ required.
    echo.
    pause
    exit /b 1
)
echo OK

echo.
echo [2/4] Setting up virtual environment...
if not exist ".venv" (
    echo Creating venv...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo ERROR: Failed to create venv.
        echo.
        pause
        exit /b 1
    )
)
echo OK

echo.
echo [3/4] Installing dependencies...
call .venv\Scripts\activate.bat
if %errorlevel% neq 0 (
    echo ERROR: Failed to activate venv.
    echo.
    pause
    exit /b 1
)
python -m pip install -q --upgrade pip
python -m pip install -q -e .
if %errorlevel% neq 0 (
    echo ERROR: Failed to install packages.
    echo.
    pause
    exit /b 1
)
echo OK

echo.
echo [4/4] Starting Streamlit app...
echo.
echo Opening http://localhost:8501 in browser.
echo Press Ctrl+C to stop.
echo.

streamlit run streamlit_app.py

echo.
echo App stopped.
pause