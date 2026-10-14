@echo off
REM Runs the Naukri auto-apply from this folder using the project's virtualenv.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" naukri_apply.py
) else (
    py naukri_apply.py
)
pause
