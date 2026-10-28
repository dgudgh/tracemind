@echo off
cd /d %~dp0..

echo Clean exe-test static...
if exist exe-test\static rmdir /s /q exe-test\static
mkdir exe-test\static

echo Build frontend...
cd frontend
call npm install
call npm run build

cd /d %~dp0..
echo Copy frontend dist to exe-test...
xcopy build\frontend-dist\* exe-test\static\ /E /I /Y

echo Build exe...
cd backend
set BACKEND_PORT=18080
set UVICORN_RELOAD=false
set SERVE_STATIC=true
set STATIC_DIR=..\exe-test\static

pyinstaller -F run.py --name TraceMindAI-Test

pause
