"""Launcher used at Windows startup: runs server.py hidden and restarts it if it ever crashes."""
import os
import subprocess
import time

BASE = os.path.dirname(os.path.abspath(__file__))
PYTHONW = os.path.join(BASE, "venv", "Scripts", "pythonw.exe")

while True:
    code = subprocess.call([PYTHONW, os.path.join(BASE, "server.py")], cwd=BASE)
    if code == 0:       # clean exit (e.g. another instance is already running)
        break
    time.sleep(5)
