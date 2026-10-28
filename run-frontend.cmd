@echo off
cd /d "%~dp0frontend"
set FRONTEND_HOST=0.0.0.0
set FRONTEND_PORT=3300
set BACKEND_HOST=127.0.0.1
set BACKEND_PORT=8800
node serve-3000.cjs
