@echo off
REM Simple OpenSearch Starter (No NSSM Required)
REM Start OpenSearch directly for development/testing

echo ================================================================================
echo Starting OpenSearch (Development Mode)
echo ================================================================================
echo.

REM ============================================================================
REM PATH CONFIGURATION - Update PROJECT_ROOT if the project is moved to another system
REM ============================================================================
set PROJECT_ROOT=%~dp0..
set BIN_DIR=%~dp0

REM Set OpenSearch installation directory (bundled in bin/opensearch-2.12.0)
set OPENSEARCH_HOME=%BIN_DIR%opensearch-2.12.0

REM Check if OpenSearch exists
if not exist "%OPENSEARCH_HOME%\bin\opensearch.bat" (
    echo ERROR: OpenSearch not found at %OPENSEARCH_HOME%
    echo.
    echo Please ensure OpenSearch is present in:
    echo   %BIN_DIR%opensearch-2.12.0
    echo.
    pause
    exit /b 1
)

REM Create necessary directories
mkdir "%PROJECT_ROOT%\runtime\opensearch\data" 2>nul
mkdir "%PROJECT_ROOT%\runtime\logs\opensearch" 2>nul
mkdir "%PROJECT_ROOT%\runtime\temp\opensearch" 2>nul

REM Set environment variables (use bundled JDK)
set OPENSEARCH_JAVA_HOME=%OPENSEARCH_HOME%\jdk
set OPENSEARCH_PATH_CONF=%OPENSEARCH_HOME%\config
set PATH=%OPENSEARCH_JAVA_HOME%\bin;%PATH%

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
