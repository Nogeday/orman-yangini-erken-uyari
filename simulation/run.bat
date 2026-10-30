@echo off
rem Windows sarmalayici: simulation\run.bat [--source demo ...]
cd /d "%~dp0.."
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 simulation\run.py %*
) else (
  python simulation\run.py %*
)
if errorlevel 1 pause
