@echo off
chcp 65001 >nul
cd /d "%~dp0"
title PC Bridge - agent console
if not exist ".venv\Scripts\python.exe" (
  echo Run START.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" server\console.py
