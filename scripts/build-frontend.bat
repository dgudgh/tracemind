@echo off
cd /d %~dp0..\frontend

echo Installing dependencies...
call npm install

echo Building frontend...
call npm run build

echo Done. Output: build/frontend-dist/
pause
