@echo off
cd /d "%~dp0"
echo Starting Motive Safety Monitoring Console...
.venv\Scripts\python.exe run.py %*
pause
