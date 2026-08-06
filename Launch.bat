@echo off
title AkiMelody
echo ========================================
echo    AkiMelody - Music Player
echo ========================================
echo.

:: Check Python
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python not found in PATH.
    echo Please install Python 3.8+ and add it to PATH.
    pause
    exit /b 1
)

:: Ensure dependencies are installed
python -c "import flask" >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [INFO] Installing Python dependencies...
    pip install -r requirements.txt >nul 2>&1
)

echo [INFO] Launching AkiMelody (WebView2)...
python webview_launcher.py
exit /b %ERRORLEVEL%
