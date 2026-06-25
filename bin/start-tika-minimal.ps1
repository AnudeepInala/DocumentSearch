# Start Tika Servers - MINIMAL VERSION (2 servers only)
# Optimized for 16GB RAM / i5 system

# ============================================================================
# PATH CONFIGURATION - Update $ProjectRoot if the project is moved to another system
# ============================================================================
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BinDir = $PSScriptRoot

# Use bundled JDK from OpenSearch
$BundledJdk = "$BinDir\opensearch-2.12.0\jdk"
$JavaExe = "$BundledJdk\bin\java.exe"
if (Test-Path $JavaExe) {
    $env:JAVA_HOME = $BundledJdk
    $env:Path = "$BundledJdk\bin;$env:Path"
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Starting Apache Tika Servers (Minimal - 2 instances)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Check if Java is available
try {
    $javaVersion = & $JavaExe -version 2>&1 | Select-String "version"
    Write-Host "Java found: $javaVersion" -ForegroundColor Green
}
catch {
    Write-Host "Java not found! Bundled JDK missing at: $BundledJdk" -ForegroundColor Red
    exit 1
}

# Get the Tika JAR path (bundled in bin/tika)
$tikaJar = Join-Path $BinDir "tika\tika-server-2.9.2.jar"

if (-not (Test-Path $tikaJar)) {
    Write-Host "Tika JAR not found at: $tikaJar" -ForegroundColor Red
    exit 1
}

Write-Host "Tika JAR found: $tikaJar" -ForegroundColor Green
Write-Host ""
Write-Host "Starting 2 Tika instances (minimal configuration)..." -ForegroundColor Yellow
Write-Host ""

# Start only 2 Tika instances for minimal system
$ports = @(9998, 9999)

$count = 1
foreach ($port in $ports) {
    Write-Host "[$count/2] Starting Tika on port $port..." -ForegroundColor Cyan
    
    # Start Tika with reduced memory (1GB instead of 2GB)
    Start-Process -FilePath $JavaExe -ArgumentList "-Xmx1024m", "-jar", $tikaJar, "--port", $port -WindowStyle Minimized
    
    Start-Sleep -Seconds 2
    $count++
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Tika servers started!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Tika instances running on ports:" -ForegroundColor White
Write-Host "  - 9998 (1GB RAM)" -ForegroundColor White
Write-Host "  - 9999 (1GB RAM)" -ForegroundColor White
Write-Host ""
Write-Host "Waiting 10 seconds for servers to initialize..." -ForegroundColor Yellow
Start-Sleep -Seconds 10

Write-Host ""
Write-Host "Verifying Tika servers..." -ForegroundColor Yellow
Write-Host ""

# Verify each instance
foreach ($port in $ports) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:$port/tika" -TimeoutSec 3 -ErrorAction Stop
        Write-Host "Port $port : Running" -ForegroundColor Green
    }
    catch {
        Write-Host "Port $port : Not responding yet (may still be starting)" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Tika startup complete!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Total memory used: ~2GB (2 x 1GB)" -ForegroundColor Cyan
Write-Host ""
