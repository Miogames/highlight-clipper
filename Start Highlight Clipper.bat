@echo off
rem Start the upgraded source version. Keep this file beside clipper_gui.py.
cd /d "%~dp0"
start "" pyw -3.12 "%~dp0clipper_gui.py"
