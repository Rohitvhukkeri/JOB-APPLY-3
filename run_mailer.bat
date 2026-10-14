@echo off
REM Emails your resume to recruiters' LinkedIn hiring posts that match your skills.
REM Add SMTP_USER and SMTP_APP_PASSWORD to .env first; without them it only saves drafts.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" linkedin_post_mailer.py %*
) else (
    py linkedin_post_mailer.py %*
)
pause
