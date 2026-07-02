# Restart Streamlit Dashboard with fresh code
Write-Host "Stopping existing Streamlit processes..." -ForegroundColor Yellow

# Stop all streamlit processes
Get-Process | Where-Object {$_.ProcessName -eq "streamlit" -or $_.MainWindowTitle -like "*Streamlit*"} | ForEach-Object {
    Write-Host "  Stopping process $($_.Id)..." -ForegroundColor Gray
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2

# Clear Python cache
Write-Host "Clearing Python cache..." -ForegroundColor Yellow
Get-ChildItem -Path "$PSScriptRoot\src" -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Starting Streamlit dashboard..." -ForegroundColor Green
Set-Location "$PSScriptRoot"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "streamlit run src/ui/dashboard.py"

Write-Host "`nDashboard is starting! Wait a few seconds then open your browser." -ForegroundColor Green
Write-Host "If the dashboard doesn't auto-open, go to: http://localhost:8501" -ForegroundColor Cyan
