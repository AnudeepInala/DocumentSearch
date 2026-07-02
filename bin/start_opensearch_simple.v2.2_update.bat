@echo off
REM Simple OpenSearch Starter (No NSSM Required)
REM Start OpenSearch directly for development/testing

echo ================================================================================
echo Starting OpenSearch (Development Mode)
echo ================================================================================
echo.

REM Set OpenSearch installation directory
set OPENSEARCH_HOME=C:\opensearch-2.12.0

REM Check if OpenSearch exists
if not exist "%OPENSEARCH_HOME%\bin\opensearch.bat" (
    echo ERROR: OpenSearch not found at %OPENSEARCH_HOME%
    echo.
    echo Please:
    echo   1. Download OpenSearch 2.x from https://opensearch.org/downloads.html
    echo   2. Extract to C:\opensearch-2.12.0
    echo   3. Or update OPENSEARCH_HOME in this script to your installation path
    echo.
    pause
    exit /b 1
)

REM Create necessary directories
mkdir "D:\DocumentSearch\opensearch\data" 2>nul
mkdir "D:\DocumentSearch\logs\opensearch" 2>nul
mkdir "D:\DocumentSearch\temp\opensearch" 2>nul

REM Set environment variables
set OPENSEARCH_JAVA_HOME=%OPENSEARCH_HOME%\jdk
set OPENSEARCH_PATH_CONF=%OPENSEARCH_HOME%\config

REM Set JVM options (12GB heap)
set OPENSEARCH_JAVA_OPTS=-Xms12g -Xmx12g

echo Starting OpenSearch...
echo This will take 30-60 seconds to fully initialize.
echo.
echo Once you see "Node started", OpenSearch is ready.
echo Press Ctrl+C to stop OpenSearch.
echo.
echo ================================================================================
echo.

cd /d "%OPENSEARCH_HOME%\bin"
call opensearch.bat

pause
