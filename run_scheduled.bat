@echo off
REM Unattended daily run (Windows Task Scheduler task "JobApply Daily"):
REM Naukri apply, then LinkedIn Easy Apply only -- no mailer / connection
REM searches, to keep LinkedIn search traffic low. LinkedIn skips itself while
REM linkedin_paused_until.txt is in the future. Output goes to daily_log.txt.
cd /d "%~dp0"
set "PY=py"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
set "JOB_APPLY_UNATTENDED=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

echo. >> daily_log.txt
echo ==================== %date% %time% ==================== >> daily_log.txt
echo --- Naukri auto-apply --- >> daily_log.txt
"%PY%" naukri_apply.py < NUL >> daily_log.txt 2>&1
echo --- LinkedIn Easy Apply --- >> daily_log.txt
"%PY%" linkedin_apply.py < NUL >> daily_log.txt 2>&1
echo --- finished %time% --- >> daily_log.txt
