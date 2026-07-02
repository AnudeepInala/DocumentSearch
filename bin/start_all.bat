@echo off
REM ============================================================================
REM Enterprise Document Search System - Start All Services
REM Complete startup script for 128 vCPU / 64GB RAM AWS Instance
REM ============================================================================

REM Check for administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo ================================================================================
    echo   ERROR: Administrator privileges required!
    echo ================================================================================
    echo.
    echo This script needs to start Windows services via NSSM.
    echo Please right-click start_all.bat and select "Run as administrator"
    echo.
    pause
    exit /b 1
)

echo.
echo ================================================================================
echo   ENTERPRISE DOCUMENT SEARCH SYSTEM
echo   AWS Instance: 128 vCPU / 64GB RAM
echo   Target: 2M documents indexed in 2-3 hours
echo ================================================================================
echo.

REM Set working directory
cd /d C:\DocumentSearch

echo [1/4] Starting OpenSearch (12GB heap)...
echo ----------------------------------------
call bin\start_opensearch.bat
if errorlevel 1 (
    echo ERROR: Failed to start OpenSearch
    pause
    exit /b 1
)

echo.
echo [2/4] Starting 8 Tika instances (16GB total)...
echo ----------------------------------------
call bin\start_tika.bat
if errorlevel 1 (
    echo ERROR: Failed to start Tika instances
    pause
    exit /b 1
)

echo.
echo [3/4] Verifying all services...
echo ----------------------------------------
timeout /t 10 /nobreak >nul

REM Verify OpenSearch
curl -s http://localhost:9200 >nul 2>&1
if errorlevel 1 (
    echo ERROR: OpenSearch not responding
    pause
    exit /b 1
)
echo ✓ OpenSearch: OK

REM Verify Tika instances
set TIKA_OK=0
for %%p in (9998 9999 10000 10001 10002 10003 10004 10005) do (
    curl -s http://localhost:%%p/tika >nul 2>&1
    if not errorlevel 1 (
        set /a TIKA_OK+=1
    )
)
echo ✓ Tika instances: %TIKA_OK%/8 running
if %TIKA_OK% LSS 6 (
    echo WARNING: Only %TIKA_OK% of 8 Tika instances running
    echo You can continue, but performance will be reduced
    pause
)

echo.
echo [4/4] Starting Master Orchestrator...
echo ----------------------------------------
echo.
echo System will start:
echo   - 4 Discovery workers
echo   - 100 Extraction workers (40+30+20+10 across pools)
echo   - 16 Indexing workers
echo   - 30 OCR workers
echo.
echo Total: 150 parallel workers
echo.
echo This will take 30-60 seconds to start all workers...
echo Press Ctrl+C at any time to stop gracefully
echo.
pause

REM Start orchestrator
set PYTHONPATH=C:\DocumentSearch\src
python C:\DocumentSearch\src\orchestrator.py

echo.
echo System stopped.
pause
