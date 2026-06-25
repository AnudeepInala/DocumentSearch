# Full System Reset Script
# This script stops all processes and performs a complete reset

# ============================================================================
# PATH CONFIGURATION - Update $ProjectRoot if the project is moved to another system
# ============================================================================
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BinDir = $PSScriptRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Full System Reset" -ForegroundColor Cyan  
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Step 1: Stop Streamlit dashboard
Write-Host "Step 1: Stopping Streamlit dashboard..." -ForegroundColor Yellow
Get-Process | Where-Object {$_.ProcessName -eq "streamlit"} | Stop-Process -Force -ErrorAction SilentlyContinue
Get-Process | Where-Object {$_.MainWindowTitle -like "*Streamlit*"} | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Write-Host "  Done" -ForegroundColor Green

# Step 2: Stop any Python processes related to DocumentSearch
Write-Host "Step 2: Stopping related Python processes..." -ForegroundColor Yellow
Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -like "*DocumentSearch*" -or $_.CommandLine -like "*DocumentSearch*"
} | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Write-Host "  Done" -ForegroundColor Green

# Step 3: Clear Python cache
Write-Host "Step 3: Clearing Python cache..." -ForegroundColor Yellow
Get-ChildItem -Path "$ProjectRoot\src" -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  Done" -ForegroundColor Green

# Step 4: Run the reset command
Write-Host "Step 4: Running system reset..." -ForegroundColor Yellow
Set-Location "$ProjectRoot"
& "$ProjectRoot\.venv\Scripts\python.exe" src/main.py reset --force

# Step 5: Verify reset
Write-Host ""
Write-Host "Step 5: Verifying reset..." -ForegroundColor Yellow
$queueDb = "$ProjectRoot\runtime\queue\queues.db"
if (Test-Path $queueDb) {
    Write-Host "  WARNING: Queue database still exists!" -ForegroundColor Red
    Write-Host "  Attempting force delete..." -ForegroundColor Yellow
    Remove-Item $queueDb -Force -ErrorAction SilentlyContinue
    Remove-Item "$queueDb-wal" -Force -ErrorAction SilentlyContinue
    Remove-Item "$queueDb-shm" -Force -ErrorAction SilentlyContinue
    Remove-Item "$queueDb-journal" -Force -ErrorAction SilentlyContinue
    if (Test-Path $queueDb) {
        Write-Host "  ERROR: Could not delete database. Please close all applications and try again." -ForegroundColor Red
    } else {
        Write-Host "  Force deleted successfully" -ForegroundColor Green
    }
} else {
    Write-Host "  Queue database cleared successfully" -ForegroundColor Green
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Reset Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Start dashboard: streamlit run src/ui/dashboard.py" -ForegroundColor Gray
Write-Host "  2. Start processing: python src/main.py start" -ForegroundColor Gray
