@echo off
rem Double-click to turn tcg_price_checker.py into "TCG Price Checker.exe".
rem Needs Python 3.10+ from python.org. Takes a few minutes; nothing is installed into your own Python.
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/ and try again.
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" || (echo Python 3.10 or newer is needed. & pause & exit /b 1)
%PY% tcg_price_checker.py --build-app
if errorlevel 1 (echo. & echo The build failed; the messages above say why.) else (echo. & echo Built: TCG Price Checker.exe & explorer /select,"%~dp0TCG Price Checker.exe")
pause
