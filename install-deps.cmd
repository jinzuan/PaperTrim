@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10+ and enable "Add Python to PATH".
  pause
  exit /b 1
)

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo Dependencies installed. Double-click the launcher to start PaperTrim.
pause
