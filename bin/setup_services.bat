@echo off
REM ============================================================================
REM Enterprise Document Search - Install All Windows Services via NSSM
REM Run this ONCE to register all services
REM ============================================================================

REM Check for administrator privileges
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo ================================================================================
    echo   ERROR: Administrator privileges required!
    echo ================================================================================
    echo.
    echo This script installs Windows services via NSSM.
    echo Please right-click setup_services.bat and select "Run as administrator"
    echo.
    pause
    exit /b 1
)

echo.
echo ================================================================================
echo   INSTALLING ENTERPRISE DOCUMENT SEARCH SERVICES
echo ================================================================================
echo.

REM Set paths
set NSSM="%~dp0nssm\nssm.exe"
set OPENSEARCH_HOME=%~dp0opensearch
set TIKA_JAR=C:\softwares\tika\tika-server-2.9.2.jar

REM Verify NSSM exists
if not exist %NSSM% (
    echo ERROR: NSSM not found at %NSSM%
    pause
    exit /b 1
)

REM Verify OpenSearch exists
if not exist "%OPENSEARCH_HOME%\bin\opensearch.bat" (
    echo ERROR: OpenSearch not found at %OPENSEARCH_HOME%
    pause
    exit /b 1
)

REM Verify Tika JAR exists
if not exist "%TIKA_JAR%" (
    echo ERROR: Tika JAR not found at %TIKA_JAR%
    pause
    exit /b 1
)

REM Create necessary directories
echo Creating directories...
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\opensearch\data" 2>nul
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\logs\opensearch" 2>nul
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika" 2>nul
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\temp\opensearch" 2>nul
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika1" 2>nul
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika2" 2>nul
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika3" 2>nul
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika4" 2>nul
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika5" 2>nul
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika6" 2>nul
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika7" 2>nul
mkdir "c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika8" 2>nul

echo.
echo ================================================================================
echo [1/9] OpenSearch Service (Skipping - Using existing OpenSearch2)
echo ================================================================================

echo ✓ Using existing OpenSearch2 service
echo   Note: If you need to reconfigure OpenSearch2, use 'nssm edit OpenSearch2'

echo.
echo ================================================================================
echo [2/9] Installing Tika-9998 Service
echo ================================================================================

%NSSM% stop Tika-9998 >nul 2>&1
%NSSM% remove Tika-9998 confirm >nul 2>&1

%NSSM% install Tika-9998 "java" "-Xmx2g" "-Xms2g" "-Djava.io.tmpdir=c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika1" "-jar" "%TIKA_JAR%" "--host=localhost" "--port=9998"
%NSSM% set Tika-9998 DisplayName "Enterprise Document Search - Tika 9998"
%NSSM% set Tika-9998 Description "Tika text extraction server on port 9998"
%NSSM% set Tika-9998 Start SERVICE_AUTO_START
%NSSM% set Tika-9998 AppStdout "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-9998-service.log"
%NSSM% set Tika-9998 AppStderr "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-9998-service-error.log"
%NSSM% set Tika-9998 AppRotateFiles 1
%NSSM% set Tika-9998 AppRotateBytes 10485760

echo ✓ Tika-9998 service installed

echo.
echo ================================================================================
echo [3/9] Installing Tika-9999 Service
echo ================================================================================

%NSSM% stop Tika-9999 >nul 2>&1
%NSSM% remove Tika-9999 confirm >nul 2>&1

%NSSM% install Tika-9999 "java" "-Xmx2g" "-Xms2g" "-Djava.io.tmpdir=c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika2" "-jar" "%TIKA_JAR%" "--host=localhost" "--port=9999"
%NSSM% set Tika-9999 DisplayName "Enterprise Document Search - Tika 9999"
%NSSM% set Tika-9999 Description "Tika text extraction server on port 9999"
%NSSM% set Tika-9999 Start SERVICE_AUTO_START
%NSSM% set Tika-9999 AppStdout "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-9999-service.log"
%NSSM% set Tika-9999 AppStderr "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-9999-service-error.log"
%NSSM% set Tika-9999 AppRotateFiles 1
%NSSM% set Tika-9999 AppRotateBytes 10485760

echo ✓ Tika-9999 service installed

echo.
echo ================================================================================
echo [4/9] Installing Tika-10000 Service
echo ================================================================================

%NSSM% stop Tika-10000 >nul 2>&1
%NSSM% remove Tika-10000 confirm >nul 2>&1

%NSSM% install Tika-10000 "java" "-Xmx2g" "-Xms2g" "-Djava.io.tmpdir=c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika3" "-jar" "%TIKA_JAR%" "--host=localhost" "--port=10000"
%NSSM% set Tika-10000 DisplayName "Enterprise Document Search - Tika 10000"
%NSSM% set Tika-10000 Description "Tika text extraction server on port 10000"
%NSSM% set Tika-10000 Start SERVICE_AUTO_START
%NSSM% set Tika-10000 AppStdout "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10000-service.log"
%NSSM% set Tika-10000 AppStderr "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10000-service-error.log"
%NSSM% set Tika-10000 AppRotateFiles 1
%NSSM% set Tika-10000 AppRotateBytes 10485760

echo ✓ Tika-10000 service installed

echo.
echo ================================================================================
echo [5/9] Installing Tika-10001 Service
echo ================================================================================

%NSSM% stop Tika-10001 >nul 2>&1
%NSSM% remove Tika-10001 confirm >nul 2>&1

%NSSM% install Tika-10001 "java" "-Xmx2g" "-Xms2g" "-Djava.io.tmpdir=c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika4" "-jar" "%TIKA_JAR%" "--host=localhost" "--port=10001"
%NSSM% set Tika-10001 DisplayName "Enterprise Document Search - Tika 10001"
%NSSM% set Tika-10001 Description "Tika text extraction server on port 10001"
%NSSM% set Tika-10001 Start SERVICE_AUTO_START
%NSSM% set Tika-10001 AppStdout "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10001-service.log"
%NSSM% set Tika-10001 AppStderr "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10001-service-error.log"
%NSSM% set Tika-10001 AppRotateFiles 1
%NSSM% set Tika-10001 AppRotateBytes 10485760

echo ✓ Tika-10001 service installed

echo.
echo ================================================================================
echo [6/9] Installing Tika-10002 Service
echo ================================================================================

%NSSM% stop Tika-10002 >nul 2>&1
%NSSM% remove Tika-10002 confirm >nul 2>&1

%NSSM% install Tika-10002 "java" "-Xmx2g" "-Xms2g" "-Djava.io.tmpdir=c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika5" "-jar" "%TIKA_JAR%" "--host=localhost" "--port=10002"
%NSSM% set Tika-10002 DisplayName "Enterprise Document Search - Tika 10002"
%NSSM% set Tika-10002 Description "Tika text extraction server on port 10002"
%NSSM% set Tika-10002 Start SERVICE_AUTO_START
%NSSM% set Tika-10002 AppStdout "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10002-service.log"
%NSSM% set Tika-10002 AppStderr "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10002-service-error.log"
%NSSM% set Tika-10002 AppRotateFiles 1
%NSSM% set Tika-10002 AppRotateBytes 10485760

echo ✓ Tika-10002 service installed

echo.
echo ================================================================================
echo [7/9] Installing Tika-10003 Service
echo ================================================================================

%NSSM% stop Tika-10003 >nul 2>&1
%NSSM% remove Tika-10003 confirm >nul 2>&1

%NSSM% install Tika-10003 "java" "-Xmx2g" "-Xms2g" "-Djava.io.tmpdir=c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika6" "-jar" "%TIKA_JAR%" "--host=localhost" "--port=10003"
%NSSM% set Tika-10003 DisplayName "Enterprise Document Search - Tika 10003"
%NSSM% set Tika-10003 Description "Tika text extraction server on port 10003"
%NSSM% set Tika-10003 Start SERVICE_AUTO_START
%NSSM% set Tika-10003 AppStdout "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10003-service.log"
%NSSM% set Tika-10003 AppStderr "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10003-service-error.log"
%NSSM% set Tika-10003 AppRotateFiles 1
%NSSM% set Tika-10003 AppRotateBytes 10485760

echo ✓ Tika-10003 service installed

echo.
echo ================================================================================
echo [8/9] Installing Tika-10004 Service
echo ================================================================================

%NSSM% stop Tika-10004 >nul 2>&1
%NSSM% remove Tika-10004 confirm >nul 2>&1

%NSSM% install Tika-10004 "java" "-Xmx2g" "-Xms2g" "-Djava.io.tmpdir=c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika7" "-jar" "%TIKA_JAR%" "--host=localhost" "--port=10004"
%NSSM% set Tika-10004 DisplayName "Enterprise Document Search - Tika 10004"
%NSSM% set Tika-10004 Description "Tika text extraction server on port 10004"
%NSSM% set Tika-10004 Start SERVICE_AUTO_START
%NSSM% set Tika-10004 AppStdout "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10004-service.log"
%NSSM% set Tika-10004 AppStderr "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10004-service-error.log"
%NSSM% set Tika-10004 AppRotateFiles 1
%NSSM% set Tika-10004 AppRotateBytes 10485760

echo ✓ Tika-10004 service installed

echo.
echo ================================================================================
echo [9/9] Installing Tika-10005 Service
echo ================================================================================

%NSSM% stop Tika-10005 >nul 2>&1
%NSSM% remove Tika-10005 confirm >nul 2>&1

%NSSM% install Tika-10005 "java" "-Xmx2g" "-Xms2g" "-Djava.io.tmpdir=c:\Users\DELL\Music\DocumentSearch\runtime\temp\tika8" "-jar" "%TIKA_JAR%" "--host=localhost" "--port=10005"
%NSSM% set Tika-10005 DisplayName "Enterprise Document Search - Tika 10005"
%NSSM% set Tika-10005 Description "Tika text extraction server on port 10005"
%NSSM% set Tika-10005 Start SERVICE_AUTO_START
%NSSM% set Tika-10005 AppStdout "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10005-service.log"
%NSSM% set Tika-10005 AppStderr "c:\Users\DELL\Music\DocumentSearch\runtime\logs\tika\tika-10005-service-error.log"
%NSSM% set Tika-10005 AppRotateFiles 1
%NSSM% set Tika-10005 AppRotateBytes 10485760

echo ✓ Tika-10005 service installed

echo.
echo ================================================================================
echo   ALL SERVICES INSTALLED SUCCESSFULLY!
echo ================================================================================
echo.
echo Services installed:
echo   • OpenSearch (12GB heap)
echo   • Tika-9998 through Tika-10005 (8 instances, 2GB each)
echo.
echo Next steps:
echo   1. Verify services in Windows Services (services.msc)
echo   2. Run: bin\start_all.bat (as administrator)
echo.
echo Services are set to AUTO START but are not running yet.
echo Use start_all.bat to start them all at once.
echo.
pause
