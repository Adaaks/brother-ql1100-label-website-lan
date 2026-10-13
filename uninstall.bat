@echo off
rem Stops labelprinter and removes it from Windows startup and the firewall. Your files stay where they are.
call "%~dp0stop.bat"
powershell -NoProfile -Command "Unregister-ScheduledTask -TaskName labelprinter -Confirm:$false -ErrorAction SilentlyContinue"
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\labelprinter.lnk" 2>nul
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -Command Remove-NetFirewallRule -DisplayName ''labelprinter (HTTP)'' -ErrorAction SilentlyContinue'"
echo labelprinter removed from startup. You can now delete this folder.
pause
