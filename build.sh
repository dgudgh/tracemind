#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================"
echo "  MESGuard DB - 一键打包"
echo "============================================"
echo ""

echo "[1/4] 检查依赖..."
command -v python3 >/dev/null 2>&1 || { echo "[错误] 找不到 Python3"; exit 1; }
command -v node >/dev/null 2>&1 || { echo "[错误] 找不到 Node.js"; exit 1; }
python3 -c "import PyInstaller" 2>/dev/null || { echo "[安装 PyInstaller]"; pip3 install pyinstaller; }

echo ""
echo "[2/4] 构建前端..."
cd "$SCRIPT_DIR/frontend"
npm install
npm run build

echo ""
echo "[3/4] 复制静态文件..."
cd "$SCRIPT_DIR"
rm -rf backend/static
cp -r frontend/dist backend/static

echo ""
echo "[4/4] PyInstaller 打包后端..."
cd "$SCRIPT_DIR/backend"
pip3 install -r requirements.txt -q
cd "$SCRIPT_DIR"
pyinstaller mesguard.spec --noconfirm --clean

echo ""
echo "[5/5] 复制启动脚本..."
cp start.sh dist/mesguard/start.sh
chmod +x dist/mesguard/start.sh

echo ""
echo "============================================"
echo "  打包完成！"
echo "  输出目录: dist/mesguard/"
echo "  启动方式: ./start.sh"
echo "============================================"
