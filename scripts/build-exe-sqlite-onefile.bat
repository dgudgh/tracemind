@echo off
chcp 65001 >nul
set PYTHONUTF8=1
if not defined PYTHON_EXE set "PYTHON_EXE=D:\python3\python.exe"

echo ===========================================
echo TraceMindAI OneFile EXE 打包（SQLite）
echo ===========================================

set SCRIPT_DIR=%~dp0
for %%I in ("%SCRIPT_DIR%..") do set "BASE_DIR=%%~fI"
if not defined EXE_DIR set "EXE_DIR=%BASE_DIR%\exe-test-onefile"
if not defined RELEASE_VERSION set "RELEASE_VERSION=1.0.0"
set "RELEASE_DIR=%EXE_DIR%\release"

echo 清理临时目录: %EXE_DIR%
if exist "%EXE_DIR%" rmdir /s /q "%EXE_DIR%"
if exist "%EXE_DIR%" exit /b 1
mkdir "%EXE_DIR%"
icacls "%EXE_DIR%" /grant "%USERNAME%:(OI)(CI)M" /T >nul 2>nul
mkdir "%EXE_DIR%\config"
mkdir "%EXE_DIR%\data"
mkdir "%EXE_DIR%\logs"
mkdir "%EXE_DIR%\db"

echo.
echo [1/3] 构建前端...
cd /d "%BASE_DIR%\frontend"
if not defined SKIP_FRONTEND_BUILD (
  if not exist "%BASE_DIR%\frontend\node_modules" call npm install
  if errorlevel 1 exit /b 1
  call npm run build
  if errorlevel 1 exit /b 1
)

echo.
echo [2/3] 生成配置文件...
(
echo APP_MODE=exe_sqlite
echo FRONTEND_PORT=3300
echo BACKEND_PORT=8800
echo DB_TYPE=sqlite
echo DATABASE_URL=sqlite:///./data/mesguard.db
echo SYSTEM_INITIALIZED=false
) > "%EXE_DIR%\config\app.env"

echo.
echo [3/3] 打包 OneFile EXE ...
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
  "%BASE_DIR%\scripts\TraceMindAI-sqlite-onefile.spec"
if errorlevel 1 exit /b %errorlevel%

if exist "%EXE_DIR%\dist\TraceMindAI.exe" (
  copy "%EXE_DIR%\dist\TraceMindAI.exe" "%EXE_DIR%\TraceMindAI.exe" /Y
) else if exist "%EXE_DIR%\dist\TraceMindAI\TraceMindAI.exe" (
  xcopy "%EXE_DIR%\dist\TraceMindAI\*" "%EXE_DIR%\" /E /I /Y
) else (
  echo 未找到 TraceMindAI.exe
  exit /b 1
)

echo.
echo [4/4] 生成可校验发布包...
mkdir "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%\config"
mkdir "%EXE_DIR%\release-manager-spec"
copy "%EXE_DIR%\TraceMindAI.exe" "%RELEASE_DIR%\TraceMindAI.exe" /Y
copy "%EXE_DIR%\config\app.env" "%RELEASE_DIR%\config\app.env.example" /Y

"%PYTHON_EXE%" -m PyInstaller ^
  --clean ^
  --noconfirm ^
  --onefile ^
  --name TraceMindRelease ^
  --paths "%BASE_DIR%\backend" ^
  --distpath "%RELEASE_DIR%" ^
  --workpath "%EXE_DIR%\release-manager-build" ^
  --specpath "%EXE_DIR%\release-manager-spec" ^
  "%BASE_DIR%\scripts\release_manager.py"
if errorlevel 1 exit /b %errorlevel%

if defined RELEASE_SIGNING_PRIVATE_KEY (
  if not defined RELEASE_TRUSTED_PUBLIC_KEY (
    echo RELEASE_SIGNING_PRIVATE_KEY 和 RELEASE_TRUSTED_PUBLIC_KEY 必须同时设置
    exit /b 1
  )
  "%PYTHON_EXE%" "%BASE_DIR%\scripts\release_manager.py" manifest ^
    --source "%RELEASE_DIR%" ^
    --version "%RELEASE_VERSION%" ^
    --platform windows ^
    --arch amd64 ^
    --entrypoint "TraceMindAI.exe" ^
    --signing-private-key "%RELEASE_SIGNING_PRIVATE_KEY%"
  if errorlevel 1 exit /b 1
  "%PYTHON_EXE%" "%BASE_DIR%\scripts\release_manager.py" verify ^
    --source "%RELEASE_DIR%" ^
    --trusted-public-key "%RELEASE_TRUSTED_PUBLIC_KEY%" ^
    --require-signature >nul
  if errorlevel 1 exit /b 1
) else (
  if defined RELEASE_TRUSTED_PUBLIC_KEY (
    echo RELEASE_SIGNING_PRIVATE_KEY 和 RELEASE_TRUSTED_PUBLIC_KEY 必须同时设置
    exit /b 1
  )
  "%PYTHON_EXE%" "%BASE_DIR%\scripts\release_manager.py" manifest ^
    --source "%RELEASE_DIR%" ^
    --version "%RELEASE_VERSION%" ^
    --platform windows ^
    --arch amd64 ^
    --entrypoint "TraceMindAI.exe"
  if errorlevel 1 exit /b %errorlevel%
  "%PYTHON_EXE%" "%BASE_DIR%\scripts\release_manager.py" verify --source "%RELEASE_DIR%" >nul
  if errorlevel 1 exit /b %errorlevel%
)

powershell -NoProfile -Command "Compress-Archive -Path '%RELEASE_DIR%\*' -DestinationPath '%EXE_DIR%\TraceMindAI-Windows-%RELEASE_VERSION%.zip' -Force"
if errorlevel 1 exit /b %errorlevel%

echo.
echo ===========================================
echo 打包完成：%EXE_DIR%\TraceMindAI.exe
echo 正式发布目录：%RELEASE_DIR%
echo 正式发布压缩包：%EXE_DIR%\TraceMindAI-Windows-%RELEASE_VERSION%.zip
echo 需要一起发布的目录：config / data / logs / db
echo ===========================================
echo 运行：%EXE_DIR%\TraceMindAI.exe
echo.
if not defined NO_PAUSE pause
