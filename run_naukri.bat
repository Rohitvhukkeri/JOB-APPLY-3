@echo off
REM Naukri: searches your skills and applies (same as run_apply.bat) from this folder using the project's virtualenv.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" naukri_apply.py
) else (
    py naukri_apply.py
)
pause
