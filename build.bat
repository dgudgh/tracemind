@echo off
chcp 65001 >nul 2>&1
title MESGuard DB - 打包构建

echo ============================================
echo   MESGuard DB - 一键打包
echo ============================================
echo.

echo [1/4] 检查依赖...
where python >nul 2>&1 || (echo [错误] 找不到 Python & pause & exit /b 1)
where node >nul 2>&1 || (echo [错误] 找不到 Node.js & pause & exit /b 1)
python -c "import PyInstaller" 2>nul || (echo [安装 PyInstaller] & pip install pyinstaller)

echo.
echo [2/4] 构建前端...
cd /d "%~dp0frontend"
call npm install
call npm run build
if errorlevel 1 (echo [错误] 前端构建失败 & pause & exit /b 1)

echo.
echo [3/4] 复制静态文件...
cd /d "%~dp0"
if exist "backend\static" rmdir /s /q "backend\static"
xcopy /e /i /q "build\frontend-dist" "backend\static"

echo.
echo [4/4] PyInstaller 打包后端...
cd /d "%~dp0backend"
pip install -r requirements.txt -q
cd /d "%~dp0"
pyinstaller mesguard.spec --noconfirm --clean
if errorlevel 1 (echo [错误] 打包失败 & pause & exit /b 1)

echo.
echo [5/5] 复制启动脚本...
copy /y "start.bat" "dist\mesguard\start.bat"
copy /y "start.sh" "dist\mesguard\start.sh"

echo.
echo ============================================
echo   打包完成！
echo   输出目录: dist\mesguard\
echo   启动方式: 双击 start.bat
echo ============================================
pause
