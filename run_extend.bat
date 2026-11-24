@echo off
REM Daily automation: extend the active library loans.
REM Run by Task Scheduler (see setup_daily_schedule.ps1). Output appended to extend_loans.log.
set SCRIPT_DIR=%~dp0
pushd "%SCRIPT_DIR%"
chcp 65001 >nul 2>&1
C:\Users\Elisha\AppData\Local\Programs\Python\Python313\python.exe -X utf8 "extend_loans.py" >> "extend_loans.log" 2>&1
popd
exit /b 0
