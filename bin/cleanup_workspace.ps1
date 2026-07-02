# Cleanup Workspace script
# Run this script in your PowerShell console to remove all redundant legacy/temporary
# files and directories, keeping the project structure clean.

$ErrorActionPreference = "Continue"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Cleaning Up Workspace Folder" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$RedundantPaths = @(
    # Nested duplicate repository folder
    "$PSScriptRoot\DocumentSearch",
    
    # 227+ legacy check/test scripts and logs folder
    "$PSScriptRoot\Test",
    
    # Obsolete audit reports and plans in root
    "$PSScriptRoot\COMPREHENSIVE_CODE_ANALYSIS.md",
    "$PSScriptRoot\CRITICAL_ISSUES_QUICK_FIX.md",
    "$PSScriptRoot\FIXES_SUMMARY.md",
    "$PSScriptRoot\SEARCH_IMPROVEMENTS.md",
    
    # Temporary streamlit / cache logs in root
    "$PSScriptRoot\.streamlit_out.log"
)

foreach ($path in $RedundantPaths) {
    if (Test-Path $path) {
        try {
            Remove-Item -Path $path -Recurse -Force -ErrorAction Stop
            Write-Host "Successfully deleted: $path" -ForegroundColor Green
        } catch {
            Write-Host "⚠ Could not delete: $path ($($_.Exception.Message))" -ForegroundColor Yellow
        }
    } else {
        Write-Host "Path not present (already clean): $path" -ForegroundColor Gray
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Cleanup complete! Folder structure is now clean." -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
