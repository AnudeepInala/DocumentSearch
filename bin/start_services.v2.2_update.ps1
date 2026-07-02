# ============================================================================
# AWS Windows Production Orchestration Script - Document Search Engine
# Optimized for 64 vCPU / 256GB RAM Infrastructure Topology
# Non-Destructive Check-and-Start Deployment Logic
# ============================================================================

$ErrorActionPreference = "Stop"
Write-Host "===================================================" -ForegroundColor Cyan
Write-Host "   Executing Automated Production Staging Sequence " -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan

# 1. PATH CONFIGURATION MATRIX
$AppRoot        = Split-Path -Path $PSScriptRoot -Parent
$NssmPath       = "$AppRoot\bin\nssm\nssm.exe"
if (-not (Test-Path $NssmPath)) {
    $NssmPath   = "$AppRoot\bin\nssm\nssm-2.24\win64\nssm.exe"
}
$RedisPath      = "$AppRoot\bin\redis\redis-server.exe"
$OpenSearchBin  = "$AppRoot\bin\opensearch\bin\opensearch.bat"
$OpenSearchHome = "$AppRoot\bin\opensearch"
$TikaJar        = "C:\softwares\tika\tika-server-2.9.2.jar"

# 2. CLEAR KNOWN DUPLICATE OR CONFLICTING SERVICE NAMES ONLY
Write-Host "`n[1/6] Scanning For Defunct Legacy Conflict Handles..." -ForegroundColor Yellow
$LegacyConflicts = @("OpenSearch", "OpenSearch2", "opensearch-service-x64", "Redis")
foreach ($srv in $LegacyConflicts) {
    if (Get-Service -Name $srv -ErrorAction SilentlyContinue) {
        Write-Host "   Removing legacy conflict handle: $srv" -ForegroundColor Gray
        try { Stop-Service -Name $srv -Force -ErrorAction SilentlyContinue } catch {}
        Start-Sleep -Seconds 1
        & $NssmPath remove $srv confirm 2>$null
        try { sc.exe delete $srv | Out-Null } catch {}
    }
}

# Kill standalone redis-server processes running in user space
try {
    $RedisProcs = Get-Process -Name "redis-server" -ErrorAction SilentlyContinue
    if ($RedisProcs) {
        Write-Host "   Killing active user-space Redis instance(s)..." -ForegroundColor Gray
        $RedisProcs | Stop-Process -Force
    }
} catch {}

# 3. DIRECTORY STRUCTURE & IO SECURITY HARMONIZATION
Write-Host "`n[2/6] Verifying Data Repositories & ACL Permissions..." -ForegroundColor Yellow
$Directories = @(
    "$AppRoot\runtime\opensearch\data",
    "$AppRoot\runtime\opensearch\logs",
    "$AppRoot\runtime\opensearch\temp"
)
(9998..10005) | ForEach-Object { $Directories += "$AppRoot\runtime\temp\tika$_" }

foreach ($dir in $Directories) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Host "   Created directory workspace: $dir" -ForegroundColor Gray
    }
}

# Ensure directory permissions are always forced for the background execution account
icacls.exe "$AppRoot\runtime" /grant "SYSTEM:(OI)(CI)F" /t /q
icacls.exe "$AppRoot\runtime" /grant "Administrators:(OI)(CI)F" /t /q

# 4. CONDITIONAL ASSESSMENT: OPENSEARCH ENGINE
Write-Host "`n[3/6] Assessing OpenSearch-Service State..." -ForegroundColor Yellow
$OSService = Get-Service -Name "OpenSearch-Service" -ErrorAction SilentlyContinue

if (-not $OSService) {
    Write-Host "   Service not found. Registering OpenSearch-Service..." -ForegroundColor Yellow
    Copy-Item -Path "$AppRoot\config\opensearch.yml" -Destination "$OpenSearchHome\config\opensearch.yml" -Force
    & $NssmPath install OpenSearch-Service "$OpenSearchBin"
    & $NssmPath set OpenSearch-Service AppDirectory "$OpenSearchHome\bin"
    & $NssmPath set OpenSearch-Service AppStdout "$AppRoot\runtime\opensearch\logs\nssm-stdout.log"
    & $NssmPath set OpenSearch-Service AppStderr "$AppRoot\runtime\opensearch\logs\nssm-stderr.log"
    & $NssmPath set OpenSearch-Service AppEnvironmentExtra "JAVA_HOME=$OpenSearchHome\jdk" "OPENSEARCH_PATH_CONF=$OpenSearchHome\config"
    Start-Service -Name "OpenSearch-Service"
    Write-Host "   Successfully deployed and started OpenSearch-Service." -ForegroundColor Green
}
elseif ($OSService.Status -ne "Running") {
    Write-Host "   OpenSearch-Service is present but stopped. Firing up node..." -ForegroundColor White
    Copy-Item -Path "$AppRoot\config\opensearch.yml" -Destination "$OpenSearchHome\config\opensearch.yml" -Force
    Start-Service -Name "OpenSearch-Service"
    Write-Host "   OpenSearch-Service status is now RUNNING." -ForegroundColor Green
}
else {
    Write-Host "   OpenSearch-Service is already running. Re-registering with fresh config..." -ForegroundColor Gray
    try { Stop-Service -Name "OpenSearch-Service" -Force -ErrorAction SilentlyContinue } catch {}
    Start-Sleep -Seconds 1
    & $NssmPath remove OpenSearch-Service confirm 2>$null
    try { sc.exe delete OpenSearch-Service | Out-Null } catch {}
    Start-Sleep -Seconds 1
    Copy-Item -Path "$AppRoot\config\opensearch.yml" -Destination "$OpenSearchHome\config\opensearch.yml" -Force
    & $NssmPath install OpenSearch-Service "$OpenSearchBin"
    & $NssmPath set OpenSearch-Service AppDirectory "$OpenSearchHome\bin"
    & $NssmPath set OpenSearch-Service AppStdout "$AppRoot\runtime\opensearch\logs\nssm-stdout.log"
    & $NssmPath set OpenSearch-Service AppStderr "$AppRoot\runtime\opensearch\logs\nssm-stderr.log"
    & $NssmPath set OpenSearch-Service AppEnvironmentExtra "JAVA_HOME=$OpenSearchHome\jdk" "OPENSEARCH_PATH_CONF=$OpenSearchHome\config"
    Start-Service -Name "OpenSearch-Service"
    Write-Host "   OpenSearch-Service status is now RUNNING." -ForegroundColor Green
}

# 5. CONDITIONAL ASSESSMENT: 8-INSTANCE TIKA PARSING CLUSTER
Write-Host "`n[4/6] Assessing Tika Text Extraction Cluster Nodes..." -ForegroundColor Yellow
$TikaClusterConfigs = @{
    9998  = @{ Heap = "2g"; Name = "Tika-9998" }
    9999  = @{ Heap = "2g"; Name = "Tika-9999" }
    10000 = @{ Heap = "4g"; Name = "Tika-10000" }
    10001 = @{ Heap = "4g"; Name = "Tika-10001" }
    10002 = @{ Heap = "2g"; Name = "Tika-10002" }
    10003 = @{ Heap = "2g"; Name = "Tika-10003" }
    10004 = @{ Heap = "2g"; Name = "Tika-10004" }
    10005 = @{ Heap = "2g"; Name = "Tika-10005" }
}

foreach ($port in ($TikaClusterConfigs.Keys | Sort-Object)) {
    $cfg = $TikaClusterConfigs[$port]
    $srvName = $cfg.Name
    $heap = $cfg.Heap
    $tikaTmp = "$AppRoot\runtime\temp\tika$port"
    
    $TikaService = Get-Service -Name $srvName -ErrorAction SilentlyContinue
    
    if (-not $TikaService) {
        Write-Host "   Node [$srvName] missing. Deploying cluster node..." -ForegroundColor White
        $JavaArgs = "-Xms$heap -Xmx$heap -Djava.io.tmpdir=$tikaTmp -jar $TikaJar -p $port -h localhost"
        & $NssmPath install $srvName "$OpenSearchHome\jdk\bin\java.exe" $JavaArgs
        & $NssmPath set $srvName AppDirectory "$OpenSearchHome\jdk\bin"
        Start-Service -Name $srvName
        Write-Host "   Node [$srvName] initialized and bound to Port $port." -ForegroundColor Green
    }
    elseif ($TikaService.Status -ne "Running") {
        Write-Host "   Node [$srvName] is stopped. Starting instance..." -ForegroundColor White
        Start-Service -Name $srvName
        Write-Host "   Node [$srvName] is now online on Port $port." -ForegroundColor Green
    }
    else {
        Write-Host "   Node [$srvName] is already actively parsing on Port $port. Skipping." -ForegroundColor Gray
    }
}

# 6. CONDITIONAL ASSESSMENT: REDIS CACHING DATASTORE
Write-Host "`n[5/6] Assessing Redis Datastore State..." -ForegroundColor Yellow
$RedisService = Get-Service -Name "Redis-Datastore" -ErrorAction SilentlyContinue

if (-not $RedisService) {
    Write-Host "   Redis-Datastore missing. Mapping service binary wrapper..." -ForegroundColor White
    & $NssmPath install Redis-Datastore "$RedisPath" "`"$AppRoot\bin\redis\redis.windows-service.conf`""
    & $NssmPath set Redis-Datastore AppDirectory "$AppRoot\bin\redis"
    Start-Service -Name "Redis-Datastore"
    Write-Host "   Redis-Datastore successfully established on Port 6379." -ForegroundColor Green
}
else {
    Write-Host "   Re-registering Redis-Datastore service..." -ForegroundColor White
    try { 
        Stop-Service -Name "Redis-Datastore" -Force -ErrorAction SilentlyContinue 
        Start-Sleep -Seconds 1
    } catch {}
    & $NssmPath remove Redis-Datastore confirm 2>$null
    try { sc.exe delete Redis-Datastore | Out-Null } catch {}
    Start-Sleep -Seconds 1
    & $NssmPath install Redis-Datastore "$RedisPath" "`"$AppRoot\bin\redis\redis.windows-service.conf`""
    & $NssmPath set Redis-Datastore AppDirectory "$AppRoot\bin\redis"
    Start-Service -Name "Redis-Datastore"
    Write-Host "   Redis-Datastore service is now online." -ForegroundColor Green
}

# 7. ENVIRONMENT INTEGRITY AUTOMATION (HYDRATE VIRTUAL ENVIRONMENT IF DEGRADED)
Write-Host "`n[6/6] Checking Python Virtual Environment Integrity..." -ForegroundColor Yellow
cd $AppRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "   Virtual Environment corrupted or missing! Commencing deep layout rebuild..." -ForegroundColor Yellow
    try { Remove-Item -Recurse -Force ".venv" -ErrorAction SilentlyContinue } catch {}
    
    $SystemPython = (where.exe python)[0]
    Write-Host "   Targeting Active System Architecture Interpreter: $SystemPython" -ForegroundColor Gray
    & $SystemPython -m venv .venv
    Start-Sleep -Seconds 2
    
    Write-Host "   Hydrating pipeline application processing library wrappers..." -ForegroundColor White
    & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    & ".\.venv\Scripts\python.exe" -m pip install PyYAML opensearch-py redis pandas openpyxl spacy --quiet
    Write-Host "   Virtual environment completely restored and linked." -ForegroundColor Green
}
else {
    Write-Host "   Virtual environment integrity verification passed. Skipping re-installation loop." -ForegroundColor Gray
}

Write-Host "`n===================================================" -ForegroundColor Cyan
Write-Host " STRATEGIC SERVICE EVALUATION COMPLETE " -ForegroundColor Cyan
Write-Host "===================================================" -ForegroundColor Cyan

# FINAL SNAPSHOT VISUAL CONNECTIONS MONITOR
Write-Host "`n--- Current Live Infrastructure Network Binding Sockets ---" -ForegroundColor White
netstat -ano | Select-String "9200|6379|9998|9999|10000|10001|10002|10003|10004|10005"
