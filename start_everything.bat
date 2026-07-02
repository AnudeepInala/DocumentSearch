@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"
set "ROOT=%CD%"
set "PADDLE_PDX_CACHE_HOME=C:\softwares\paddle_models"
set "PYTHON_BIN=%ROOT%\.venv\Scripts\python.exe"
set "TIKA_JAR=C:\softwares\tika\tika-server-2.9.2.jar"
set "TIKA_CFG=%ROOT%\config\tika-config.xml"
rem JVM flags shared by all Tika instances:
rem  -Dfile.encoding=UTF-8           : force UTF-8 I/O so font glyph names decode correctly
rem  -Dsun.java2d.cmm=sun.java2d.cmm.kcms.KcmsServiceProvider : faster ICC color profile
rem    loading used by PDFBox for embedded-font rendering on Windows
rem  -Djava.awt.headless=true        : no GUI required; prevents AWT hangs on Windows Server
set "TIKA_JVM_COMMON=-Dfile.encoding=UTF-8 -Dsun.java2d.cmm=sun.java2d.cmm.kcms.KcmsServiceProvider -Djava.awt.headless=true"

if not exist "%PYTHON_BIN%" (
  set "PYTHON_BIN=python"
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
if not exist "%ROOT%\runtime\temp\opensearch" mkdir "%ROOT%\runtime\temp\opensearch"
if not exist "%ROOT%\runtime\opensearch\data" mkdir "%ROOT%\runtime\opensearch\data"
if not exist "%ROOT%\runtime\opensearch\logs" mkdir "%ROOT%\runtime\opensearch\logs"

echo Starting backend services (using local copies)...
if exist "%ROOT%\bin\redis\redis-server.exe" (
  start "Redis" cmd /c "\"%ROOT%\bin\redis\redis-server.exe\" --port 6380 > \"%ROOT%\runtime\logs\redis.log\" 2>&1"
) else (
  where redis-server >nul 2>&1
  if not errorlevel 1 start "Redis" cmd /c "redis-server --port 6380 > \"%ROOT%\runtime\logs\redis.log\" 2>&1"
)

if exist "%ROOT%\bin\opensearch\bin\opensearch.bat" (
  echo Generating OpenSearch config from templates...
  powershell -Command "$r = '%ROOT%'; $rf = $r.Replace('\','/'); (Get-Content \"$r\config\opensearch.yml\") -replace '\{app_root\}', $rf | Set-Content \"$r\bin\opensearch\config\opensearch.yml\""
  powershell -Command "$r = '%ROOT%'; (Get-Content \"$r\config\jvm.options\") -replace '\{app_root\}', $r.Replace('\','/') | Set-Content \"$r\bin\opensearch\config\jvm.options\""
  start "OpenSearch" cmd /c "\"%ROOT%\bin\opensearch\bin\opensearch.bat\" > \"%ROOT%\runtime\logs\opensearch.log\" 2>&1"
) else (
  where opensearch >nul 2>&1
  if not errorlevel 1 start "OpenSearch" cmd /c "opensearch > \"%ROOT%\runtime\logs\opensearch.log\" 2>&1"
)

echo Starting Tika instances...
netstat -ano | findstr ":9908 " >nul 2>&1
if errorlevel 1 (
  start /B "" cmd /c "java -Xms768m -Xmx768m %TIKA_JVM_COMMON% -Djava.io.tmpdir=\"%ROOT%\runtime\temp\tika1\" -jar \"%TIKA_JAR%\" --port 9908 --config \"%TIKA_CFG%\" > \"%ROOT%\runtime\logs\tika-9908.log\" 2>&1"
  echo   Tika 9908 starting...
)
netstat -ano | findstr ":9909 " >nul 2>&1
if errorlevel 1 (
  start /B "" cmd /c "java -Xms768m -Xmx768m %TIKA_JVM_COMMON% -Djava.io.tmpdir=\"%ROOT%\runtime\temp\tika2\" -jar \"%TIKA_JAR%\" --port 9909 --config \"%TIKA_CFG%\" > \"%ROOT%\runtime\logs\tika-9909.log\" 2>&1"
  echo   Tika 9909 starting...
)
netstat -ano | findstr ":9910 " >nul 2>&1
if errorlevel 1 (
  start /B "" cmd /c "java -Xms1g -Xmx1g %TIKA_JVM_COMMON% -Djava.io.tmpdir=\"%ROOT%\runtime\temp\tika3\" -jar \"%TIKA_JAR%\" --port 9910 --config \"%TIKA_CFG%\" > \"%ROOT%\runtime\logs\tika-9910.log\" 2>&1"
  echo   Tika 9910 starting...
)
netstat -ano | findstr ":9911 " >nul 2>&1
if errorlevel 1 (
  start /B "" cmd /c "java -Xms1g -Xmx1g %TIKA_JVM_COMMON% -Djava.io.tmpdir=\"%ROOT%\runtime\temp\tika4\" -jar \"%TIKA_JAR%\" --port 9911 --config \"%TIKA_CFG%\" > \"%ROOT%\runtime\logs\tika-9911.log\" 2>&1"
  echo   Tika 9911 starting...
)

echo Waiting 30s for Tika JVMs to start...
timeout /t 30 /nobreak >nul

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
start "DocumentSearch Dashboard" cmd /k "cd /d \"%ROOT%\" && \"%PYTHON_BIN%\" -m streamlit run src\ui\dashboard.py --server.port 8502"

echo.
echo Startup complete.
echo Dashboard: http://localhost:8502
echo OpenSearch: http://localhost:9201
endlocal
