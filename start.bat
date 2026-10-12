@echo off
rem Starts the labelprinter server in the background (same as at Windows startup).
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0run.pyw"
echo labelprinter started: http://labelprinter.local/
