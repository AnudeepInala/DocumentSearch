# Restart Streamlit Dashboard with fresh code

# ============================================================================
# PATH CONFIGURATION - Update $ProjectRoot if the project is moved to another system
# ============================================================================
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BinDir = $PSScriptRoot

Write-Host "Stopping existing Streamlit processes..." -ForegroundColor Yellow

# Stop all streamlit processes
Get-Process | Where-Object {$_.ProcessName -eq "streamlit" -or $_.MainWindowTitle -like "*Streamlit*"} | ForEach-Object {
    Write-Host "  Stopping process $($_.Id)..." -ForegroundColor Gray
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2

# Clear Python cache
Write-Host "Clearing Python cache..." -ForegroundColor Yellow
Get-ChildItem -Path "$ProjectRoot\src" -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Starting Streamlit dashboard..." -ForegroundColor Green
Set-Location "$ProjectRoot"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$ProjectRoot'; & '$ProjectRoot\.venv\Scripts\python.exe' -m streamlit run src/ui/dashboard.py --server.fileWatcherType poll"

Write-Host "`nDashboard is starting! Wait a few seconds then open your browser." -ForegroundColor Green
Write-Host "If the dashboard doesn't auto-open, go to: http://localhost:8501" -ForegroundColor Cyan
