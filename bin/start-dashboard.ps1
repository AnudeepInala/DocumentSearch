# Enterprise Document Search - Dashboard Startup Script
# Run this to start the web dashboard (after the main system is running)

# ============================================================================
# PATH CONFIGURATION - Update $ProjectRoot if the project is moved to another system
# ============================================================================
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BinDir = $PSScriptRoot

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Enterprise Document Search - Dashboard" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "Starting Streamlit dashboard..." -ForegroundColor Yellow
Write-Host ""
Write-Host "The dashboard will open in your browser at:" -ForegroundColor White
Write-Host "  http://localhost:8501" -ForegroundColor Cyan
Write-Host ""
Write-Host "Press Ctrl+C to stop" -ForegroundColor Yellow
Write-Host ""

# Start the dashboard
Set-Location $ProjectRoot
& "$ProjectRoot\.venv\Scripts\python.exe" -m streamlit run src/ui/dashboard.py --server.fileWatcherType poll
