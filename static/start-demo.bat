@echo off
title EHA Registration
cd /d "%~dp0"
echo Installing dependencies (flask, reportlab)...
python -m pip install flask reportlab -q
if errorlevel 1 (
  echo.
  echo pip failed. Try: python -m pip install flask reportlab
  pause
  exit /b 1
)
echo.
echo Starting EHA Registration on all network interfaces...
echo Other devices: use this PC's IPv4 address, port 3000
echo.
python demo_server.py
if errorlevel 1 (
  echo.
  echo Server stopped with an error.
  pause
)
