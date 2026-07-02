@echo off
REM ============================================================================
REM Enterprise Document Search - Start All Tika Instances
REM Optimized for 128 vCPU / 64GB RAM AWS Instance
REM ============================================================================

REM Check for administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: Administrator privileges required to start services!
    echo Please run as administrator.
    pause
    exit /b 1
)

echo ========================================
echo Starting 7 Tika Server Instances
echo (Port 10001 disabled - blocked by agentid-service)
echo Total Memory: 14GB (7 x 2GB)
echo Ports: 9998-10000, 10002-10005
echo ========================================
echo.

REM Set NSSM path
set NSSM="%~dp0nssm\nssm.exe"

REM Set Tika JAR location (adjust path as needed)
set TIKA_JAR=C:\softwares\tika\tika-server-2.9.2.jar

REM Check if Tika JAR exists
if not exist "%TIKA_JAR%" (
    echo ERROR: Tika JAR not found at %TIKA_JAR%
    echo Please download tika-server.jar and update the path
    pause
    exit /b 1
)

REM Create temp directories
echo Creating temp directories...
mkdir "%~dp0..\runtime\temp\tika1" 2>nul
mkdir "%~dp0..\runtime\temp\tika2" 2>nul
mkdir "%~dp0..\runtime\temp\tika3" 2>nul
mkdir "%~dp0..\runtime\temp\tika4" 2>nul
mkdir "%~dp0..\runtime\temp\tika5" 2>nul
mkdir "%~dp0..\runtime\temp\tika6" 2>nul
mkdir "%~dp0..\runtime\temp\tika7" 2>nul
mkdir "%~dp0..\runtime\temp\tika8" 2>nul

REM Create logs directory
mkdir "%~dp0..\runtime\logs\tika" 2>nul

echo.
echo Starting Tika services (via NSSM)...
echo.

REM Instance 1 - Port 9998
echo [1/8] Starting Tika-9998 service...
%NSSM% start Tika-9998
timeout /t 1 /nobreak >nul

REM Instance 2 - Port 9999
echo [2/8] Starting Tika-9999 service...
%NSSM% start Tika-9999
timeout /t 1 /nobreak >nul

REM Instance 3 - Port 10000
echo [3/8] Starting Tika-10000 service...
%NSSM% start Tika-10000
timeout /t 1 /nobreak >nul

REM Instance 4 - Port 10001 (DISABLED - port blocked by agentid-service)
REM echo [4/8] Starting Tika-10001 service...
REM %NSSM% start Tika-10001
REM timeout /t 1 /nobreak >nul

REM Instance 5 - Port 10002
echo [5/8] Starting Tika-10002 service...
%NSSM% start Tika-10002
timeout /t 1 /nobreak >nul

REM Instance 6 - Port 10003
echo [6/8] Starting Tika-10003 service...
%NSSM% start Tika-10003
timeout /t 1 /nobreak >nul

REM Instance 7 - Port 10004
echo [7/8] Starting Tika-10004 service...
%NSSM% start Tika-10004
timeout /t 1 /nobreak >nul

REM Instance 8 - Port 10005
echo [8/8] Starting Tika-10005 service...
%NSSM% start Tika-10005

echo.
echo ========================================
echo Waiting 15 seconds for all instances to start...
echo ========================================
timeout /t 15 /nobreak >nul

echo.
echo Verifying Tika instances...
echo.

REM Verify each instance
call :CHECK_TIKA 9998
call :CHECK_TIKA 9999
call :CHECK_TIKA 10000
REM call :CHECK_TIKA 10001  (DISABLED - port blocked by agentid-service)
call :CHECK_TIKA 10002
call :CHECK_TIKA 10003
call :CHECK_TIKA 10004
call :CHECK_TIKA 10005

echo.
echo ========================================
echo All 7 Tika instances started!
echo Total Memory Allocated: 14GB
echo ========================================
echo.
echo Logs location: %~dp0..\runtime\logs\tika\
echo.
echo To stop all instances, run: stop_tika.bat
echo.

pause
exit /b 0

:CHECK_TIKA
set PORT=%1
curl -s -o nul -w "Port %PORT%: %%{http_code}\n" http://localhost:%PORT%/tika
exit /b 0
