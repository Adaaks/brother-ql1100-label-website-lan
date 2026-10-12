@echo off
rem Lets other devices on your home network reach labelprinter (port 80, Private networks only).
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process '%~f0' -Verb RunAs" & exit /b)
powershell -NoProfile -Command "Remove-NetFirewallRule -DisplayName 'labelprinter (HTTP)' -ErrorAction SilentlyContinue; New-NetFirewallRule -DisplayName 'labelprinter (HTTP)' -Direction Inbound -Protocol TCP -LocalPort 80,8080 -Profile Private -Action Allow | Out-Null"
echo Firewall rule added. Other devices on your network can now open http://labelprinter.local/
pause
