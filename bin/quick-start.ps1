# Quick Start Script for Document Search System
# Run this script to set up the environment and start the system

# ============================================================================
# PATH CONFIGURATION - Update $ProjectRoot if the project is moved to another system
# ============================================================================
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BinDir = $PSScriptRoot

# Use bundled JDK from OpenSearch (no external Java install needed)
$BundledJdk = "$BinDir\opensearch-2.12.0\jdk"
$JavaExe = "$BundledJdk\bin\java.exe"

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "Enterprise Document Search System - Quick Start" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host ""

# Set Java environment
Write-Host "[1/6] Setting up Java environment..." -ForegroundColor Yellow
if (Test-Path $JavaExe) {
    $env:JAVA_HOME = $BundledJdk
    $env:Path = "$BundledJdk\bin;$env:Path"
    Write-Host "✓ Using bundled JDK: $BundledJdk" -ForegroundColor Green
} else {
    Write-Host "✗ Bundled JDK not found at $BundledJdk" -ForegroundColor Red
    exit 1
}

# Verify Java
$javaVersion = & $JavaExe -version 2>&1 | Select-String "version"
if ($javaVersion) {
    Write-Host "✓ Java is ready: $javaVersion" -ForegroundColor Green
}
else {
    Write-Host "✗ Java not found! Bundled JDK missing." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[2/6] Checking OpenSearch..." -ForegroundColor Yellow
try {
    Invoke-WebRequest -Uri "http://localhost:9200" -TimeoutSec 5 -ErrorAction Stop | Out-Null
    Write-Host "✓ OpenSearch is running" -ForegroundColor Green
}
catch {
    Write-Host "✗ OpenSearch is not running!" -ForegroundColor Red
    Write-Host "  Please start OpenSearch first:" -ForegroundColor Yellow
    Write-Host "  cd $BinDir\opensearch-2.12.0\bin" -ForegroundColor Yellow
    Write-Host "  .\opensearch.bat" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "[3/6] Checking Tika servers..." -ForegroundColor Yellow
try {
    Invoke-WebRequest -Uri "http://localhost:9998/tika" -TimeoutSec 5 -ErrorAction Stop | Out-Null
    Write-Host "✓ Tika servers are running" -ForegroundColor Green
}
catch {
    Write-Host "⚠ Tika servers not detected" -ForegroundColor Yellow
    Write-Host "  You need to start Tika servers manually in a separate terminal:" -ForegroundColor Yellow
    Write-Host "  cd $BinDir" -ForegroundColor Cyan
    Write-Host "  .\start_tika.bat" -ForegroundColor Cyan
    Write-Host ""
}

Write-Host ""
Write-Host "[4/6] Checking system initialization..." -ForegroundColor Yellow
if (Test-Path "$ProjectRoot\runtime\queue\queues.db") {
    Write-Host "✓ System already initialized" -ForegroundColor Green
}
else {
    Write-Host "  Initializing system..." -ForegroundColor Yellow
    Set-Location $ProjectRoot
    & "$ProjectRoot\.venv\Scripts\python.exe" src/main.py init
}

Write-Host ""
Write-Host "[5/6] Running system check..." -ForegroundColor Yellow
Set-Location $ProjectRoot
& "$ProjectRoot\.venv\Scripts\python.exe" src/main.py check

Write-Host ""
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "System Ready!" -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Choose an option:" -ForegroundColor Yellow
Write-Host "  1. Start document processing" -ForegroundColor White
Write-Host "  2. Open dashboard only" -ForegroundColor White
Write-Host "  3. View system status" -ForegroundColor White
Write-Host "  4. Exit" -ForegroundColor White
Write-Host ""

$choice = Read-Host "Enter choice (1-4)"

switch ($choice) {
    "1" {
        Write-Host ""
        Write-Host "[6/6] Starting document processing..." -ForegroundColor Yellow
        Write-Host "Press Ctrl+C to stop" -ForegroundColor Yellow
        Write-Host ""
        Set-Location $ProjectRoot
        & "$ProjectRoot\.venv\Scripts\python.exe" src/main.py start
    }
    "2" {
        Write-Host ""
        Write-Host "[6/6] Opening dashboard..." -ForegroundColor Yellow
        Write-Host "Dashboard will open at http://localhost:8501" -ForegroundColor Cyan
        Write-Host ""
        Set-Location $ProjectRoot
        & "$ProjectRoot\.venv\Scripts\python.exe" -m streamlit run src/ui/dashboard.py --server.fileWatcherType poll
    }
    "3" {
        Write-Host ""
        Set-Location $ProjectRoot
        & "$ProjectRoot\.venv\Scripts\python.exe" src/main.py status
        Write-Host ""
        & "$ProjectRoot\.venv\Scripts\python.exe" src/main.py stats
    }
    "4" {
        Write-Host "Goodbye!" -ForegroundColor Cyan
        exit 0
    }
    default {
        Write-Host "Invalid choice. Exiting." -ForegroundColor Red
        exit 1
    }
}
