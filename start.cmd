@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found: .venv\Scripts\python.exe
  echo See README.md for setup instructions.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 tools\serve.py --open
if errorlevel 1 pause
