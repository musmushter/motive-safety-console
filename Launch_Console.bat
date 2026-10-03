@echo off
title Motive Safety Console
cd /d "%~dp0"
echo ========================================================
echo   Starting Motive Safety Monitoring Console
echo   Organizations: HEART (260129) ^& Houston Towing (599530)
echo ========================================================
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe run.py
) else (
    python run.py
)
pause
