#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv-mac-primary/bin/python}"
DIST_DIR="${ROOT_DIR}/dist/macos"
WORK_DIR="${ROOT_DIR}/build/pyinstaller-macos"
APP_PATH="${DIST_DIR}/TraceMind AI.app"
PACKAGE_DIR="${DIST_DIR}/TraceMindAI-macOS-arm64"
ZIP_PATH="${DIST_DIR}/TraceMindAI-macOS-arm64.zip"
DMG_PATH="${DIST_DIR}/TraceMindAI-macOS-arm64.dmg"
RELEASE_VERSION="${RELEASE_VERSION:-$("${PYTHON_BIN}" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "${ROOT_DIR}/frontend/package.json")}"
RELEASE_SIGNING_PRIVATE_KEY="${RELEASE_SIGNING_PRIVATE_KEY:-}"
RELEASE_TRUSTED_PUBLIC_KEY="${RELEASE_TRUSTED_PUBLIC_KEY:-}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Python environment not found: ${PYTHON_BIN}" >&2
  exit 1
fi

cd "${ROOT_DIR}/frontend"
if command -v pnpm >/dev/null 2>&1; then
  pnpm run build
else
  npm run build
fi

cd "${ROOT_DIR}"
"${PYTHON_BIN}" scripts/create_macos_icon.py

rm -rf "${WORK_DIR}" "${DIST_DIR}"
mkdir -p "${DIST_DIR}"

"${PYTHON_BIN}" -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath "${DIST_DIR}" \
  --workpath "${WORK_DIR}" \
  scripts/TraceMindAI-macos.spec

/usr/bin/codesign --force --deep --sign - "${APP_PATH}"
/usr/bin/codesign --verify --deep --strict --verbose=2 "${APP_PATH}"

mkdir -p "${PACKAGE_DIR}/config"
mkdir -p "${WORK_DIR}/release-manager-spec"
/usr/bin/ditto "${APP_PATH}" "${PACKAGE_DIR}/TraceMind AI.app"

"${PYTHON_BIN}" -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --name TraceMindRelease \
  --paths "${ROOT_DIR}/backend" \
  --distpath "${PACKAGE_DIR}" \
  --workpath "${WORK_DIR}/release-manager" \
  --specpath "${WORK_DIR}/release-manager-spec" \
  "${ROOT_DIR}/scripts/release_manager.py"
/usr/bin/codesign --force --sign - "${PACKAGE_DIR}/TraceMindRelease"
/usr/bin/codesign --verify --strict --verbose=2 "${PACKAGE_DIR}/TraceMindRelease"

printf '%s\n' \
  'APP_MODE=exe_sqlite' \
  'FRONTEND_PORT=3300' \
  'BACKEND_PORT=8800' \
  'DB_TYPE=sqlite' \
  'DATABASE_URL=sqlite:///./data/mesguard.db' \
  'SYSTEM_INITIALIZED=false' \
  > "${PACKAGE_DIR}/config/app.env.example"

manifest_args=(
  manifest
  --source "${PACKAGE_DIR}"
  --version "${RELEASE_VERSION}"
  --platform macos
  --arch arm64
  --entrypoint "TraceMind AI.app/Contents/MacOS/TraceMindAI"
)
verify_args=(verify --source "${PACKAGE_DIR}")
if [[ -n "${RELEASE_SIGNING_PRIVATE_KEY}" || -n "${RELEASE_TRUSTED_PUBLIC_KEY}" ]]; then
  if [[ -z "${RELEASE_SIGNING_PRIVATE_KEY}" || -z "${RELEASE_TRUSTED_PUBLIC_KEY}" ]]; then
    echo "RELEASE_SIGNING_PRIVATE_KEY and RELEASE_TRUSTED_PUBLIC_KEY must be set together" >&2
    exit 1
  fi
  manifest_args+=(--signing-private-key "${RELEASE_SIGNING_PRIVATE_KEY}")
  verify_args+=(--trusted-public-key "${RELEASE_TRUSTED_PUBLIC_KEY}" --require-signature)
fi
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/release_manager.py" "${manifest_args[@]}"
"${PYTHON_BIN}" "${ROOT_DIR}/scripts/release_manager.py" "${verify_args[@]}" >/dev/null

/usr/bin/ditto -c -k --sequesterRsrc --keepParent "${PACKAGE_DIR}" "${ZIP_PATH}"
/usr/bin/hdiutil create \
  -volname "TraceMind AI" \
  -srcfolder "${PACKAGE_DIR}" \
  -ov \
  -format UDZO \
  "${DMG_PATH}"

echo
echo "Build complete:"
echo "  APP: ${APP_PATH}"
echo "  RELEASE: ${PACKAGE_DIR}"
echo "  ZIP: ${ZIP_PATH}"
echo "  DMG: ${DMG_PATH}"
/usr/bin/shasum -a 256 "${ZIP_PATH}" "${DMG_PATH}"
