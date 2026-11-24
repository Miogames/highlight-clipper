@echo off
setlocal
cd /d "%~dp0"
echo Installing the NVIDIA libraries that let your graphics card read speech...
py -3.12 -m pip install --upgrade faster-whisper nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
if errorlevel 1 goto :failed
echo.
echo Done. Open Highlight Clipper and press "Test GPU speech" to check it.
pause
exit /b 0

:failed
echo.
echo Installation failed. Make sure Python 3.12 and an NVIDIA graphics driver are installed, then try again.
pause
exit /b 1
