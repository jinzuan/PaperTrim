@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python was not found. Please install Python 3.10 or newer:
  echo https://www.python.org/downloads/windows/
  echo Remember to enable "Add Python to PATH" during installation.
  echo.
  pause
  exit /b 1
)

python -c "import cv2, numpy, PIL, starlette, uvicorn, multipart" >nul 2>nul
if errorlevel 1 (
  echo.
  echo Missing dependencies. Please run "install-deps.cmd" first,
  echo or run: python -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

python run.py %*
if errorlevel 1 (
  echo.
  echo PaperTrim exited with an error. See the log above.
  pause
)
