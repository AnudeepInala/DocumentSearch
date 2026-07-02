@echo off
REM ============================================================================
REM Enterprise Document Search - Stop All Tika Instances
REM ============================================================================

REM Set NSSM path
set NSSM="%~dp0nssm\nssm-2.24\win64\nssm.exe"

echo ========================================
echo Stopping All Tika Instances
echo ========================================
echo.

REM Stop all Tika Windows services
echo Stopping Tika services (via NSSM)...

%NSSM% stop Tika-9998
%NSSM% stop Tika-9999
%NSSM% stop Tika-10000
%NSSM% stop Tika-10001
%NSSM% stop Tika-10002
%NSSM% stop Tika-10003
%NSSM% stop Tika-10004
%NSSM% stop Tika-10005

echo.
echo All Tika instances stopped.
echo.

REM Clean up temp files (optional)
choice /C YN /M "Clean up temp files?"
if errorlevel 2 goto :END
if errorlevel 1 goto :CLEANUP

:CLEANUP
echo Cleaning temp directories...
rmdir /s /q "%~dp0..\runtime\temp\tika1" 2>nul
rmdir /s /q "%~dp0..\runtime\temp\tika2" 2>nul
rmdir /s /q "%~dp0..\runtime\temp\tika3" 2>nul
rmdir /s /q "%~dp0..\runtime\temp\tika4" 2>nul
rmdir /s /q "%~dp0..\runtime\temp\tika5" 2>nul
rmdir /s /q "%~dp0..\runtime\temp\tika6" 2>nul
rmdir /s /q "%~dp0..\runtime\temp\tika7" 2>nul
rmdir /s /q "%~dp0..\runtime\temp\tika8" 2>nul
echo Temp files cleaned.

:END
echo.
pause
