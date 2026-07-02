@echo off
REM Simple Tika Startup Script - No Admin Required
REM Starts 7 Tika server instances on different ports

echo ============================================================
echo Starting Apache Tika Servers
echo ============================================================
echo.

REM Check if Java is available
where java >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Java not found in PATH!
    echo Please set JAVA_HOME and add to PATH:
    echo   $env:JAVA_HOME = "C:\Program Files\Eclipse Adoptium\jdk-25.0.2.10-hotspot"
    echo   $env:Path += ";C:\Program Files\Eclipse Adoptium\jdk-25.0.2.10-hotspot\bin"
    pause
    exit /b 1
)

REM Get the Tika JAR path
set TIKA_JAR=C:\softwares\tika\tika-server-2.9.2.jar

if not exist "%TIKA_JAR%" (
    echo ERROR: Tika JAR not found at %TIKA_JAR%
    pause
    exit /b 1
)

echo Starting 7 Tika instances...
echo.

REM Start Tika instances in background
echo [1/7] Starting Tika on port 9998...
start "Tika-9998" java -jar "%TIKA_JAR%" --port 9998

timeout /t 2 /nobreak >nul

echo [2/7] Starting Tika on port 9999...
start "Tika-9999" java -jar "%TIKA_JAR%" --port 9999

timeout /t 2 /nobreak >nul

echo [3/7] Starting Tika on port 10000...
start "Tika-10000" java -jar "%TIKA_JAR%" --port 10000

timeout /t 2 /nobreak >nul

echo [4/7] Starting Tika on port 10002...
start "Tika-10002" java -jar "%TIKA_JAR%" --port 10002

timeout /t 2 /nobreak >nul

echo [5/7] Starting Tika on port 10003...
start "Tika-10003" java -jar "%TIKA_JAR%" --port 10003

timeout /t 2 /nobreak >nul

echo [6/7] Starting Tika on port 10004...
start "Tika-10004" java -jar "%TIKA_JAR%" --port 10004

timeout /t 2 /nobreak >nul

echo [7/7] Starting Tika on port 10005...
start "Tika-10005" java -jar "%TIKA_JAR%" --port 10005

echo.
echo ============================================================
echo All Tika servers started!
echo ============================================================
echo.
echo Tika instances running on ports:
echo   - 9998, 9999, 10000, 10002, 10003, 10004, 10005
echo.
echo To verify, run in another terminal:
echo   curl http://localhost:9998/tika
echo.
echo Press any key to close this window...
pause >nul
