@echo off
rem Keep this file ASCII-only: cmd misreads multi-byte (Georgian) text in batch files.
chcp 65001 >nul
cd /d "%~dp0"

rem Find Python: "python" (skipping the Microsoft Store placeholder), else the "py" launcher.
set "PY="
python --version >nul 2>nul && set "PY=python"
if not defined PY py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY goto nopython

%PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto piperror

%PY% server.py
pause
exit /b 0

:nopython
echo.
echo  Python was not found.
echo  Install Python 3.10 or newer from https://www.python.org/downloads/
echo  and tick "Add python.exe to PATH" during setup. See README.md for details.
echo.
pause
exit /b 1

:piperror
echo.
echo  Could not install the required package edge-tts.
echo  Check the internet connection and run start.bat again.
echo.
pause
exit /b 1
