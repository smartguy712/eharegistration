@echo off
title HANDBALL REGISTRATION LOCAL SYSTEM
echo ====================================================
echo CLOUD-HYBRID LOCAL WORKSPACE PLATFORM AUTOMATOR
echo ====================================================
echo.
echo [STEP 1/3] Starting Native PostgreSQL Engine for Windows...
net start postgresql-x64-15
echo.
echo [STEP 2/3] Booting Up Server Core API Engine Modules...
start cmd /k "cd /d C:\handball-system\backend && npm run start-local"
timeout /t 3 /nobreak > nul
echo.
echo [STEP 3/3] Opening Web Interface Dashboard Panel...
start chrome "http://localhost:3000/admin/rosters"
echo.
echo ====================================================
echo System executing locally. Keep window open to log games.
echo ====================================================
pause
