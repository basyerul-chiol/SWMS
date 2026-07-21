@echo off
cd /d "%~dp0"
if not exist .env (
  echo.
  echo ERROR: .env not found.
  echo Copy your working .env into this folder, then run START_SWMS.bat again.
  echo.
  pause
  exit /b 1
)
python -m pip install -r requirements.txt
if errorlevel 1 pause & exit /b 1
python db.py
if errorlevel 1 pause & exit /b 1
python app.py
pause
