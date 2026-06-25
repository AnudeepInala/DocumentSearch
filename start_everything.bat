@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
set "ROOT=%CD%"
set "PYTHON_BIN=C:\Users\hp212560601\AppData\Local\Programs\Python\Python312\python.exe"
set "REDIS_BIN=%ROOT%\bin\Redis-x64-3.2.100\redis-server.exe"
set "OPENSEARCH_BIN=%ROOT%\bin\opensearch-2.12.0\bin\opensearch.bat"
set "JAVA_BIN=%ROOT%\bin\opensearch-2.12.0\jdk\bin\java.exe"
set "TIKA_JAR=%ROOT%\bin\tika\tika-server-2.9.2.jar"

if not exist "%PYTHON_BIN%" (
  echo Python not found at: %PYTHON_BIN%
  echo Please install Python 3.12 or update this path.
  exit /b 1
)

REM Kill any existing orchestrator/dashboard to prevent duplicates
echo Checking for existing instances...
for /f "tokens=2" %%P in ('wmic process where "commandline like '%%src/main.py start%%' or commandline like '%%src\\main.py start%%'" get processid 2^>nul ^| findstr /R "[0-9]"') do (
  taskkill /PID %%P /T /F >nul 2>&1
)
for /f "tokens=2" %%P in ('wmic process where "commandline like '%%dashboard.py%%' and commandline like '%%streamlit%%'" get processid 2^>nul ^| findstr /R "[0-9]"') do (
  taskkill /PID %%P /T /F >nul 2>&1
)

if not exist "%REDIS_BIN%" (
  echo Redis not found at: %REDIS_BIN%
  exit /b 1
)

if not exist "%OPENSEARCH_BIN%" (
  echo OpenSearch not found at: %OPENSEARCH_BIN%
  exit /b 1
)

if not exist "%JAVA_BIN%" (
  echo Java not found at: %JAVA_BIN%
  exit /b 1
)

if not exist "%TIKA_JAR%" (
  echo Tika JAR not found: %TIKA_JAR%
  exit /b 1
)

if not exist "%ROOT%\runtime\logs" mkdir "%ROOT%\runtime\logs"
if not exist "%ROOT%\runtime\temp\tika1" mkdir "%ROOT%\runtime\temp\tika1"
if not exist "%ROOT%\runtime\temp\tika2" mkdir "%ROOT%\runtime\temp\tika2"
if not exist "%ROOT%\runtime\temp\tika3" mkdir "%ROOT%\runtime\temp\tika3"
if not exist "%ROOT%\runtime\temp\tika4" mkdir "%ROOT%\runtime\temp\tika4"

echo Starting backend services (using local copies)...
start "Redis" cmd /c "\"%REDIS_BIN%\" > \"%ROOT%\runtime\logs\redis.log\" 2>&1"

start "OpenSearch" cmd /c "\"%OPENSEARCH_BIN%\" > \"%ROOT%\runtime\logs\opensearch.log\" 2>&1"

echo Starting Tika instances...
for %%P in (9998 9999 10000 10001) do (
  netstat -ano | findstr /R ":%%P .*LISTEN" >nul
  if errorlevel 1 (
    if %%P==9998 start "Tika %%P" cmd /c "\"%JAVA_BIN%\" -Xms768m -Xmx768m -Djava.io.tmpdir=\"%ROOT%\runtime\temp\tika1\" -jar \"%TIKA_JAR%\" --port %%P > \"%ROOT%\runtime\logs\tika-%%P.log\" 2>&1"
    if %%P==9999 start "Tika %%P" cmd /c "\"%JAVA_BIN%\" -Xms768m -Xmx768m -Djava.io.tmpdir=\"%ROOT%\runtime\temp\tika2\" -jar \"%TIKA_JAR%\" --port %%P > \"%ROOT%\runtime\logs\tika-%%P.log\" 2>&1"
    if %%P==10000 start "Tika %%P" cmd /c "\"%JAVA_BIN%\" -Xms1g -Xmx1g -Djava.io.tmpdir=\"%ROOT%\runtime\temp\tika3\" -jar \"%TIKA_JAR%\" --port %%P > \"%ROOT%\runtime\logs\tika-%%P.log\" 2>&1"
    if %%P==10001 start "Tika %%P" cmd /c "\"%JAVA_BIN%\" -Xms1g -Xmx1g -Djava.io.tmpdir=\"%ROOT%\runtime\temp\tika4\" -jar \"%TIKA_JAR%\" --port %%P > \"%ROOT%\runtime\logs\tika-%%P.log\" 2>&1"
  )
)

echo Running health check...
"%PYTHON_BIN%" src\main.py check
if errorlevel 1 (
  echo Health check failed. Verify Redis/OpenSearch/Tika/Java installation.
  exit /b 1
)

echo Initializing system...
"%PYTHON_BIN%" src\main.py init
if errorlevel 1 exit /b 1

echo Starting orchestrator and dashboard...
start "DocumentSearch Orchestrator" cmd /k "cd /d \"%ROOT%\" && \"%PYTHON_BIN%\" src\main.py start"
start "DocumentSearch Dashboard" cmd /k "cd /d \"%ROOT%\" && \"%PYTHON_BIN%\" -m streamlit run src\ui\dashboard.py --server.port 8501 --server.fileWatcherType poll"

echo.
echo Startup complete.
echo Dashboard: http://localhost:8501
echo OpenSearch: http://localhost:9200
endlocal
