@echo off
REM Indeed: searches your skills and applies to "Easily apply" jobs on in.indeed.com.
REM Log in once first: .venv\Scripts\python.exe login_capture.py indeed
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" indeed_apply.py
) else (
    py indeed_apply.py
)
pause
