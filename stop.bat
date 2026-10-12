@echo off
rem Stops the labelprinter server (and its auto-restart launcher) running from this folder.
powershell -NoProfile -Command "$d = '%~dp0'; Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine.Contains($d + 'run.pyw') -or $_.CommandLine.Contains($d + 'server.py')) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo labelprinter stopped.
