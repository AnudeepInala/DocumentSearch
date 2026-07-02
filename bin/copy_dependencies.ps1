$ErrorActionPreference = "Stop"

Write-Host "========================================"
Write-Host "Copying Dependencies to Project Folder"
Write-Host "========================================"
Write-Host ""

$TargetNssm = "$PSScriptRoot\bin\nssm"
$TargetRedis = "$PSScriptRoot\bin\redis"
$TargetOpenSearch = "$PSScriptRoot\bin\opensearch"
$TargetTesseract = "$PSScriptRoot\bin\tesseract"
$TargetPoppler = "$PSScriptRoot\bin\poppler"

$Targets = @($TargetNssm, $TargetRedis, $TargetOpenSearch, $TargetTesseract, $TargetPoppler)
foreach ($target in $Targets) {
    if (-not (Test-Path $target)) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        Write-Host "Created folder: $target"
    }
}

# 1. Copy NSSM
$SourceNssm = "C:\Users\DELL\Tools\nssm\nssm-2.24"
if (-not (Test-Path $SourceNssm)) {
    $SourceNssm = "C:\Users\DELL\Tools\nssm\nssm-2.24-101-g897c7ad"
}
if (Test-Path "$SourceNssm\win64\nssm.exe") {
    Write-Host "Copying NSSM..."
    Copy-Item -Path "$SourceNssm\win64\nssm.exe" -Destination "$TargetNssm\nssm.exe" -Force
    Write-Host "NSSM copied successfully"
} elseif (Test-Path $SourceNssm) {
    Write-Host "Copying NSSM..."
    Copy-Item -Path "$SourceNssm\*" -Destination $TargetNssm -Recurse -Force
    Write-Host "NSSM copied successfully"
} else {
    Write-Host "NSSM source not found at: $SourceNssm"
}

# 2. Copy Redis
$SourceRedis = "C:\Users\DELL\Downloads\Redis-x64-3.2.100"
if (-not (Test-Path $SourceRedis)) {
    $SourceRedis = "C:\Program Files\Redis"
}
if (Test-Path $SourceRedis) {
    Write-Host "Copying Redis..."
    Copy-Item -Path "$SourceRedis\*" -Destination $TargetRedis -Recurse -Force
    Write-Host "Redis copied successfully"
} else {
    Write-Host "Redis source not found"
}

# 3. Copy OpenSearch
$SourceOpenSearch = "C:\opensearch\opensearch-2.14.0"
if (Test-Path $SourceOpenSearch) {
    Write-Host "Copying OpenSearch..."
    Copy-Item -Path "$SourceOpenSearch\*" -Destination $TargetOpenSearch -Recurse -Force
    Write-Host "OpenSearch copied successfully"
} else {
    Write-Host "OpenSearch source not found at: $SourceOpenSearch"
}

# 4. Copy Tesseract
$SourceTesseract = "C:\Program Files\Tesseract-OCR"
if (Test-Path $SourceTesseract) {
    Write-Host "Copying Tesseract OCR..."
    Copy-Item -Path "$SourceTesseract\*" -Destination $TargetTesseract -Recurse -Force
    Write-Host "Tesseract OCR copied successfully"
} else {
    Write-Host "Tesseract OCR source not found at: $SourceTesseract"
}

# 5. Copy Poppler
$SourcePoppler = "C:\poppler\poppler-24.02.0\Library"
if (Test-Path $SourcePoppler) {
    Write-Host "Copying Poppler..."
    Copy-Item -Path "$SourcePoppler\*" -Destination $TargetPoppler -Recurse -Force
    Write-Host "Poppler copied successfully"
} else {
    Write-Host "Poppler source not found at: $SourcePoppler"
}

Write-Host ""
Write-Host "========================================"
Write-Host "Dependency copy complete!"
Write-Host "========================================"
