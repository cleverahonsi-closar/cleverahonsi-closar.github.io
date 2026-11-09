@echo off
rem Closar: public site at http://127.0.0.1:8810 and the private studio at http://127.0.0.1:8810/studio
cd /d "%~dp0"
start "" pythonw server.py
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8810/studio
