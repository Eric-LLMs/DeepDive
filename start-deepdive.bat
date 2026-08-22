@echo off
rem Double-click launcher for the DeepDive desktop workbench (Windows).
rem Runs scripts/start_desktop.sh (bash) which is idempotent: it starts the
rem backend on :8300 if needed, the web UI on :5173, then opens Electron.
rem The console window stays open while the app runs; close the app to exit.

cd /d "%~dp0"

set "BASH="
for %%D in (C D E F G) do (
  if exist "%%D:\Program Files\Git\bin\bash.exe"   set "BASH=%%D:\Program Files\Git\bin\bash.exe"
  if exist "%%D:\Program Files (x86)\Git\bin\bash.exe" set "BASH=%%D:\Program Files (x86)\Git\bin\bash.exe"
)
if "%BASH%"=="" (
  where bash >nul 2>nul && set "BASH=bash"
)
if "%BASH%"=="" (
  echo [ERROR] Git Bash not found. Install Git for Windows first.
  pause
  exit /b 1
)

"%BASH%" scripts/start_desktop.sh
set "EXIT=%errorlevel%"

echo.
echo DeepDive exited with code %EXIT%.
pause
exit /b %EXIT%
