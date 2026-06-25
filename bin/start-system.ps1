# Enterprise Document Search - Simple Startup Script
# Run this to start the document processing system

# ============================================================================
# PATH CONFIGURATION - Update $ProjectRoot if the project is moved to another system
# ============================================================================
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BinDir = $PSScriptRoot

# Use bundled JDK from OpenSearch (no external Java install needed)
$BundledJdk = "$BinDir\opensearch-2.12.0\jdk"
$JavaExe = "$BundledJdk\bin\java.exe"
$PythonExe = "$ProjectRoot\.venv\Scripts\python.exe"

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Enterprise Document Search System - Startup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Set Java environment
Write-Host "Setting Java environment..." -ForegroundColor Yellow
if (Test-Path $JavaExe) {
    $env:JAVA_HOME = $BundledJdk
    $env:Path = "$BundledJdk\bin;$env:Path"
    Write-Host "Using bundled JDK: $BundledJdk" -ForegroundColor Green
} else {
    Write-Host "Bundled JDK not found at: $BundledJdk" -ForegroundColor Red
    exit 1
}
Write-Host ""

if (-not (Test-Path $PythonExe)) {
    Write-Host "Python venv not found at: $PythonExe" -ForegroundColor Red
    exit 1
}

# Wait for database locks to release
Write-Host "Waiting for database locks to release..." -ForegroundColor Yellow
Start-Sleep -Seconds 3
Write-Host ""

# Start the system
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Starting Document Search System..." -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "This will:" -ForegroundColor White
Write-Host "  1. Discover all files in test_data folder" -ForegroundColor White
Write-Host "  2. Extract text from each file" -ForegroundColor White
Write-Host "  3. Index to OpenSearch" -ForegroundColor White
Write-Host "  4. Make documents searchable" -ForegroundColor White
Write-Host ""
Write-Host "Press Ctrl+C to stop" -ForegroundColor Yellow
Write-Host ""

# Start the main system
Set-Location $ProjectRoot
& $PythonExe src/main.py start
