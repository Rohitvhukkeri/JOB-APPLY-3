@echo off
REM Unattended daily run (started by Windows Task Scheduler at 9 AM):
REM refresh the Naukri profile, then Naukri apply, LinkedIn Easy Apply and the
REM LinkedIn hiring-post mailer. Each step runs even if the previous one failed.
REM Output goes to daily_log.txt.
cd /d "%~dp0"
set "PY=py"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
set "JOB_APPLY_UNATTENDED=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

echo. >> daily_log.txt
echo ==================== %date% %time% ==================== >> daily_log.txt
echo --- Naukri profile refresh --- >> daily_log.txt
"%PY%" naukri_refresh_resume.py >> daily_log.txt 2>&1
echo --- Naukri auto-apply --- >> daily_log.txt
"%PY%" naukri_apply.py >> daily_log.txt 2>&1
echo --- LinkedIn Easy Apply --- >> daily_log.txt
"%PY%" linkedin_apply.py >> daily_log.txt 2>&1
echo --- LinkedIn hiring-post mailer --- >> daily_log.txt
"%PY%" linkedin_post_mailer.py >> daily_log.txt 2>&1
echo --- finished %time% --- >> daily_log.txt
