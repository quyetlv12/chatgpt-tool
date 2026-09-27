@echo off
setlocal
set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"

echo ============================================
echo   Shoptaikhoan Tool - Change 2FA
echo   Source Edition
echo ============================================
echo.

:: --- Check Python ---
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python khong duoc tim thay.
    echo Hay cai Python 3.11+ tu https://www.python.org/downloads/
    echo va chon "Add Python to PATH" khi cai dat.
    pause
    exit /b 1
)

:: --- Create venv if missing ---
if not exist "%VENV%\Scripts\python.exe" (
    echo [1/3] Dang tao moi truong ao...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo [ERROR] Khong the tao .venv
        pause
        exit /b 1
    )
)

:: --- Install deps ---
echo [2/3] Dang cai dat dependencies...
"%VENV%\Scripts\pip.exe" install --quiet --upgrade pip
"%VENV%\Scripts\pip.exe" install --quiet -r "%ROOT%requirements-source.txt"
if errorlevel 1 (
    echo [ERROR] Cai dat dependencies that bai.
    echo Xem log phia tren de biet chi tiet.
    pause
    exit /b 1
)

:: --- CA cert ---
set "CA_DIR=%LOCALAPPDATA%\InfinityAIStore\Change2FA"
if not exist "%CA_DIR%" mkdir "%CA_DIR%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

:: --- Start server ---
echo [3/3] Dang khoi dong server...
echo.
echo   URL: http://127.0.0.1:5033
echo   Nhan Ctrl+C de dung server.
echo.
"%VENV%\Scripts\python.exe" "%ROOT%change 2fa community\server.py" --host 127.0.0.1 --port 5033

endlocal
