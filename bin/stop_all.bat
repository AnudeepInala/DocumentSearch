@echo off
REM ============================================================================
REM Enterprise Document Search System - Stop All Services
REM ============================================================================

REM Check for administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: Administrator privileges required to stop services!
    echo Please run as administrator.
    pause
    exit /b 1
)

REM ============================================================================
REM PATH CONFIGURATION - Update PROJECT_ROOT if the project is moved to another system
REM ============================================================================
set PROJECT_ROOT=%~dp0..
set BIN_DIR=%~dp0

REM Set NSSM path (bundled in bin/nssm-2.14)
set NSSM="%BIN_DIR%nssm-2.14\win64\nssm.exe"

echo.
echo ================================================================================
echo   STOPPING ENTERPRISE DOCUMENT SEARCH SYSTEM
echo ================================================================================
echo.

echo [1/4] Stopping Master Orchestrator and all workers...
echo ----------------------------------------
REM Find and kill Python processes running our scripts
for /f "tokens=2" %%a in ('tasklist ^| findstr /i "python.exe"') do (
    wmic process where "ProcessId=%%a and CommandLine like '%%main.py%%'" call terminate >nul 2>&1
    wmic process where "ProcessId=%%a and CommandLine like '%%dashboard.py%%'" call terminate >nul 2>&1
)
echo ✓ Orchestrator and Dashboard processes terminated

echo.
echo [2/4] Stopping Tika instances...
echo ----------------------------------------
call "%~dp0stop_tika.bat"
echo ✓ Tika instances stopped

echo.
echo [3/4] Stopping OpenSearch...
echo ----------------------------------------
%NSSM% stop OpenSearch-Service
echo ✓ OpenSearch stopped

echo.
echo [4/4] Stopping Redis...
echo ----------------------------------------
%NSSM% stop Redis-Datastore
echo ✓ Redis stopped

echo.
echo ================================================================================
echo   ALL SERVICES STOPPED
echo ================================================================================
echo.
echo System state saved in checkpoints.
echo Resume anytime by running: bin\start_all.bat
echo.
pause
