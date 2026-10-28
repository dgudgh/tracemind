@echo off
chcp 65001 >nul
set PYTHONUTF8=1
if not defined PYTHON_EXE set "PYTHON_EXE=D:\python3\python.exe"

echo ========================================
echo TraceMindAI EXE SQLite Build Script
echo ========================================

set SCRIPT_DIR=%~dp0
for %%I in ("%SCRIPT_DIR%..") do set "BASE_DIR=%%~fI"
if not defined EXE_DIR set "EXE_DIR=%BASE_DIR%\exe-test"

echo Clean exe-test...
if exist "%EXE_DIR%" rmdir /s /q "%EXE_DIR%"
if exist "%EXE_DIR%" exit /b 1
mkdir "%EXE_DIR%"
icacls "%EXE_DIR%" /grant "%USERNAME%:(OI)(CI)M" /T >nul 2>nul
mkdir "%EXE_DIR%\static"
mkdir "%EXE_DIR%\config"
mkdir "%EXE_DIR%\data"
mkdir "%EXE_DIR%\logs"
mkdir "%EXE_DIR%\db"

echo Build frontend...
cd /d "%BASE_DIR%\frontend"
if not defined SKIP_FRONTEND_BUILD (
  if not exist "%BASE_DIR%\frontend\node_modules" call npm install
  if errorlevel 1 exit /b %errorlevel%
  call npm run build
  if errorlevel 1 exit /b %errorlevel%
)

echo Copy frontend dist...
xcopy "%BASE_DIR%\build\frontend-dist\*" "%EXE_DIR%\static\" /E /I /Y

echo Write config...
(
echo APP_MODE=exe_sqlite
echo FRONTEND_PORT=3300
echo BACKEND_PORT=8800
echo DB_TYPE=sqlite
echo DATABASE_URL=sqlite:///./data/mesguard.db
echo SYSTEM_INITIALIZED=false
) > "%EXE_DIR%\config\app.env"

echo Build exe with PyInstaller...
cd /d "%BASE_DIR%\backend"
"%PYTHON_EXE%" -m pip install --upgrade pip -q
"%PYTHON_EXE%" -m pip install -q -r "%BASE_DIR%\backend\requirements.txt"
if errorlevel 1 exit /b %errorlevel%
"%PYTHON_EXE%" -m pip install -q -r "%BASE_DIR%\backend\requirements-build.txt"
if errorlevel 1 exit /b %errorlevel%
"%PYTHON_EXE%" -m PyInstaller ^
  --clean ^
  --noconfirm ^
  --distpath "%EXE_DIR%\dist" ^
  --workpath "%EXE_DIR%\build" ^
  "%BASE_DIR%\scripts\TraceMindAI-sqlite.spec"
if errorlevel 1 exit /b %errorlevel%

if exist "%EXE_DIR%\dist\TraceMindAI\TraceMindAI.exe" (
  xcopy "%EXE_DIR%\dist\TraceMindAI\*" "%EXE_DIR%\" /E /I /Y
  if errorlevel 1 exit /b %errorlevel%
) else (
  copy "%EXE_DIR%\dist\TraceMindAI.exe" "%EXE_DIR%\TraceMindAI.exe" /Y
  if errorlevel 1 exit /b %errorlevel%
)

echo ========================================
echo Done! EXE test package ready at:
echo %EXE_DIR%
echo ========================================
echo.
echo Run: exe-test\TraceMindAI.exe
echo Then open: http://localhost:3300
echo.
if not defined NO_PAUSE pause
