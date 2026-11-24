@echo off
setlocal
cd /d "%~dp0"
py -3.12 -m pip install --upgrade pip
if errorlevel 1 goto :failed
py -3.12 -m pip install -r requirements.txt
if errorlevel 1 goto :failed
echo.
echo Dependencies installed. Double-click "Start Highlight Clipper.bat" to launch it.
pause
exit /b 0

:failed
echo.
echo Installation failed. Install Python 3.12, reopen this window, and try again.
pause
exit /b 1
