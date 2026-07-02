# Quick Start Script for Document Search System
# Run this script to set up the environment and start the system

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "Enterprise Document Search System - Quick Start" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host ""

# Set Java environment
Write-Host "[1/6] Setting up Java environment..." -ForegroundColor Yellow
$env:JAVA_HOME = "C:\Program Files\Eclipse Adoptium\jdk-25.0.2.10-hotspot"
$env:Path += ";C:\Program Files\Eclipse Adoptium\jdk-25.0.2.10-hotspot\bin"

# Verify Java
$javaVersion = & java -version 2>&1 | Select-String "version"
if ($javaVersion) {
    Write-Host "✓ Java is ready: $javaVersion" -ForegroundColor Green
}
else {
    Write-Host "✗ Java not found! Please check installation." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[2/6] Checking OpenSearch..." -ForegroundColor Yellow
try {
    $response = Invoke-WebRequest -Uri "http://localhost:9200" -TimeoutSec 5 -ErrorAction Stop
    Write-Host "✓ OpenSearch is running" -ForegroundColor Green
}
catch {
    Write-Host "✗ OpenSearch is not running!" -ForegroundColor Red
    Write-Host "  Please start OpenSearch first:" -ForegroundColor Yellow
    Write-Host "  cd `$PSScriptRoot\bin\opensearch\bin" -ForegroundColor Yellow
    Write-Host "  .\opensearch.bat" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "[3/6] Checking Tika servers..." -ForegroundColor Yellow
$tikaRunning = $false
try {
    $response = Invoke-WebRequest -Uri "http://localhost:9998/tika" -TimeoutSec 5 -ErrorAction Stop
    Write-Host "✓ Tika servers are running" -ForegroundColor Green
    $tikaRunning = $true
}
catch {
    Write-Host "⚠ Tika servers not detected" -ForegroundColor Yellow
    Write-Host "  You need to start Tika servers manually in a separate terminal:" -ForegroundColor Yellow
    Write-Host "  cd `$PSScriptRoot\bin" -ForegroundColor Cyan
    Write-Host "  .\start_tika.bat" -ForegroundColor Cyan
    Write-Host ""
}

Write-Host ""
Write-Host "[4/6] Checking system initialization..." -ForegroundColor Yellow
if (Test-Path "`$PSScriptRoot\runtime\queue\queues.db") {
    Write-Host "✓ System already initialized" -ForegroundColor Green
}
else {
    Write-Host "  Initializing system..." -ForegroundColor Yellow
    python src/main.py init
}

Write-Host ""
Write-Host "[5/6] Running system check..." -ForegroundColor Yellow
python src/main.py check

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
        python src/main.py start
    }
    "2" {
        Write-Host ""
        Write-Host "[6/6] Opening dashboard..." -ForegroundColor Yellow
        Write-Host "Dashboard will open at http://localhost:8501" -ForegroundColor Cyan
        Write-Host ""
        streamlit run src/ui/dashboard.py
    }
    "3" {
        Write-Host ""
        python src/main.py status
        Write-Host ""
        python src/main.py stats
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
