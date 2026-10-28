@echo off
cd /d "%~dp0backend"
set BACKEND_PORT=8800
set SERVE_STATIC=false
python run.py
