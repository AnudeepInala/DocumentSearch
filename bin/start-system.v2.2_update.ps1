# Enterprise Document Search - Simple Startup Script
# Run this to start the document processing system

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Enterprise Document Search System - Startup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# Set Java environment
Write-Host "Setting Java environment..." -ForegroundColor Yellow
$env:JAVA_HOME = "C:\Program Files\Eclipse Adoptium\jdk-17.0.11.9-hotspot"
$env:Path += ";C:\Program Files\Eclipse Adoptium\jdk-17.0.11.9-hotspot\bin"
Write-Host "Java configured" -ForegroundColor Green
Write-Host ""

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
python src/main.py start
