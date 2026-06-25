# Start Tika Servers - PowerShell Script
# Starts 7 Tika server instances on different ports

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
Write-Host "Starting Apache Tika Servers" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Check if Java is available
try {
    $javaVersion = & $JavaExe -version 2>&1 | Select-String "version"
    Write-Host "✓ Java found: $javaVersion" -ForegroundColor Green
}
catch {
    Write-Host "✗ Java not found! Bundled JDK missing at: $BundledJdk" -ForegroundColor Red
    exit 1
}

# Get the Tika JAR path (bundled in bin/tika)
$tikaJar = Join-Path $BinDir "tika\tika-server-2.9.2.jar"

if (-not (Test-Path $tikaJar)) {
    Write-Host "✗ Tika JAR not found at: $tikaJar" -ForegroundColor Red
    exit 1
}

Write-Host "✓ Tika JAR found: $tikaJar" -ForegroundColor Green
Write-Host ""
Write-Host "Starting 7 Tika instances..." -ForegroundColor Yellow
Write-Host ""

# Ports to use
$ports = @(9998, 9999, 10000, 10002, 10003, 10004, 10005)

# Start each Tika instance
$count = 1
foreach ($port in $ports) {
    Write-Host "[$count/7] Starting Tika on port $port..." -ForegroundColor Cyan
    
    # Start Tika in a new window
    Start-Process -FilePath $JavaExe -ArgumentList "-jar", $tikaJar, "--port", $port -WindowStyle Minimized
    
    Start-Sleep -Seconds 2
    $count++
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "All Tika servers started!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Tika instances running on ports:" -ForegroundColor White
Write-Host "  - 9998, 9999, 10000, 10002, 10003, 10004, 10005" -ForegroundColor White
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
        Write-Host "✓ Port $port : Running" -ForegroundColor Green
    }
    catch {
        Write-Host "✗ Port $port : Not responding yet (may still be starting)" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Tika startup complete!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "To verify manually, run:" -ForegroundColor White
Write-Host "  curl http://localhost:9998/tika" -ForegroundColor Cyan
Write-Host ""
