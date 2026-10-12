# One-time setup: Python environment, start with Windows, firewall rule, then start the server.
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir

Write-Host "`n== labelprinter setup ==`n"

# 1. Python 3.10+
$py = $null
foreach ($cand in @("py -3", "python")) {
    try {
        $v = & ([scriptblock]::Create("$cand -c `"import sys; print('%d.%d' % sys.version_info[:2])`"")) 2>$null
        if ($v -and [version]$v -ge [version]"3.10") { $py = $cand; break }
    } catch {}
}
if (-not $py) {
    Write-Host "Python 3.10 or newer was not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run install.bat again."
    exit 1
}
Write-Host "Using Python $v"

# 2. Printer driver check
if (-not (Get-Printer -Name "Brother QL-1100" -ErrorAction SilentlyContinue)) {
    Write-Host "Warning: no printer called 'Brother QL-1100' is installed yet." -ForegroundColor Yellow
    Write-Host "Install Brother's QL-1100 driver from support.brother.com and keep the default printer name.`n"
}

# 3. Virtual environment + packages
if (-not (Test-Path "$dir\venv\Scripts\python.exe")) {
    Write-Host "Creating Python environment..."
    & ([scriptblock]::Create("$py -m venv `"$dir\venv`""))
}
Write-Host "Installing packages (this takes a minute)..."
& "$dir\venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$dir\venv\Scripts\python.exe" -m pip install --quiet -r "$dir\requirements.txt"

# 4. Start automatically when you log in to Windows
$startup = [Environment]::GetFolderPath("Startup")
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$startup\labelprinter.lnk")
$lnk.TargetPath = "$dir\venv\Scripts\pythonw.exe"
$lnk.Arguments = "`"$dir\run.pyw`""
$lnk.WorkingDirectory = $dir
$lnk.WindowStyle = 7
$lnk.Description = "labelprinter server"
$lnk.Save()
Write-Host "Added to Windows startup."

# 5. Firewall rule so other devices on your network can connect (asks for admin)
Write-Host "Adding the firewall rule (approve the admin prompt)..."
try { Start-Process -FilePath "$dir\allow-firewall.bat" -Verb RunAs -Wait } catch { Write-Host "Skipped - run allow-firewall.bat later as admin." -ForegroundColor Yellow }

# 6. Start it now
& "$dir\stop.bat" | Out-Null
Start-Process -FilePath "$dir\venv\Scripts\pythonw.exe" -ArgumentList "`"$dir\run.pyw`"" -WorkingDirectory $dir
Start-Sleep 4
Write-Host "`nDone! Open http://labelprinter.local on any device on your network." -ForegroundColor Green
Write-Host "(or http://<this PC's IP address> if .local names don't work on a device)`n"
