@echo off
REM ============================================================================
REM Enterprise Document Search System - Start Services Wrapper
REM ============================================================================

REM Check for administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: Administrator privileges required to configure services!
    echo Please right-click start_services.bat and select "Run as administrator".
    pause
    exit /b 1
)

cd /d "%~dp0"
echo Starting services via PowerShell script...
powershell -ExecutionPolicy Bypass -File "%~dp0bin\start_services.ps1"

if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to start services.
    pause
    exit /b %errorlevel%
)

echo.
echo All services configured and verified successfully.
pause
