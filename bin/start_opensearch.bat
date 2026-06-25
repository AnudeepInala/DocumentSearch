@echo off
REM ============================================================================
REM Enterprise Document Search - Start OpenSearch
REM Optimized with 12GB heap for 128 vCPU / 64GB RAM
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
echo Starting OpenSearch
echo Heap Size: 12GB
echo ========================================
echo.

REM ============================================================================
REM PATH CONFIGURATION - Update PROJECT_ROOT if the project is moved to another system
REM ============================================================================
set PROJECT_ROOT=%~dp0..
set BIN_DIR=%~dp0

REM Set NSSM path (bundled in bin/nssm-2.14)
set NSSM="%BIN_DIR%nssm-2.14\win64\nssm.exe"

REM Set OpenSearch installation directory (bundled in bin/opensearch-2.12.0)
set OPENSEARCH_HOME=%BIN_DIR%opensearch-2.12.0

REM Use bundled JDK from OpenSearch
set JAVA_HOME=%OPENSEARCH_HOME%\jdk
set PATH=%JAVA_HOME%\bin;%PATH%

REM Check if OpenSearch exists
if not exist "%OPENSEARCH_HOME%\bin\opensearch.bat" (
    echo ERROR: OpenSearch not found at %OPENSEARCH_HOME%
    echo Please install OpenSearch and update the path
    pause
    exit /b 1
)

REM Create necessary directories
echo Creating directories...
mkdir "%PROJECT_ROOT%\runtime\opensearch\data" 2>nul
mkdir "%PROJECT_ROOT%\runtime\logs\opensearch" 2>nul
mkdir "%PROJECT_ROOT%\runtime\temp\opensearch" 2>nul

REM Copy custom configuration files
echo Copying configuration files...
copy /Y "%PROJECT_ROOT%\config\opensearch.yml" "%OPENSEARCH_HOME%\config\opensearch.yml" >nul
copy /Y "%PROJECT_ROOT%\config\jvm.options" "%OPENSEARCH_HOME%\config\jvm.options" >nul

echo.
echo Starting OpenSearch service (via NSSM)...
echo This may take 30-60 seconds...
echo.

REM Start OpenSearch2 Windows service (existing service)
%NSSM% start OpenSearch2

echo.
echo Waiting for OpenSearch to start...
timeout /t 30 /nobreak >nul

echo.
echo Checking OpenSearch status...

REM Check if OpenSearch is responding
curl -s http://localhost:9200 >nul 2>&1
if errorlevel 1 (
    echo WARNING: OpenSearch may still be starting...
    echo Wait 30 more seconds and check http://localhost:9200
) else (
    echo.
    echo ========================================
    echo OpenSearch is running!
    echo ========================================
    echo.
    curl -s http://localhost:9200 | findstr "cluster_name version"
)

echo.
echo OpenSearch UI: http://localhost:9200
echo Logs: %~dp0..\runtime\logs\opensearch\
echo.

pause
