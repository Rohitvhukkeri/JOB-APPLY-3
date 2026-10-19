@echo off
REM Runs every automation one after another: Naukri apply, LinkedIn Easy Apply,
REM then the LinkedIn hiring-post mailer. A failure in one step never stops the next.
REM Note: if Naukri opened company-site tabs, close that browser to continue.
cd /d "%~dp0"
set "PY=py"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo ===================== Naukri auto-apply =====================
"%PY%" naukri_apply.py
echo.
echo ===================== LinkedIn Easy Apply =====================
"%PY%" linkedin_apply.py
echo.
echo ===================== LinkedIn hiring-post mailer =====================
"%PY%" linkedin_post_mailer.py
echo.
"%PY%" manual_todo.py
echo.
echo All automations finished.
pause
