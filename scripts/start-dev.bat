@echo off
title TraceMind AI Dev

echo ========================================
echo   Start TraceMind AI Dev Mode
echo   Frontend: http://127.0.0.1:3300
echo   Backend : http://127.0.0.1:8800
echo ========================================

start "backend-8800" cmd /k "cd /d %~dp0..\backend && set BACKEND_PORT=8800 && set UVICORN_RELOAD=true && set SERVE_STATIC=false && python run.py"

timeout /t 2

start "frontend-3300" cmd /k "cd /d %~dp0..\frontend && npm run dev -- --host 0.0.0.0 --port 3300"

pause
