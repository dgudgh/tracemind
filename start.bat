@echo off
chcp 65001 >nul 2>&1
title MESGuard DB - 数据库运维平台

set "APP_DIR=%~dp0"
set "APP_DIR=%APP_DIR:~0,-1%"

echo ============================================
echo   MESGuard DB - 数据库运维平台
echo ============================================
echo.

if exist "%APP_DIR%\mesguard.exe" (
    echo 正在启动服务...
    echo 访问地址: http://127.0.0.1:8800
    echo 按 Ctrl+C 停止服务
    echo.
    cd /d "%APP_DIR%"
    mesguard.exe
) else (
    echo [错误] 找不到 mesguard.exe
    echo 请确认文件完整性。
    pause
)
