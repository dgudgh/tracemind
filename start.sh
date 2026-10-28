#!/bin/bash
APP_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================"
echo "  MESGuard DB - 数据库运维平台"
echo "============================================"
echo ""

if [ -f "$APP_DIR/mesguard" ]; then
    echo "正在启动服务..."
    echo "访问地址: http://127.0.0.1:8090"
    echo "按 Ctrl+C 停止服务"
    echo ""
    cd "$APP_DIR"
    chmod +x mesguard
    ./mesguard
else
    echo "[错误] 找不到 mesguard"
    echo "请确认文件完整性。"
    exit 1
fi
