# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.win32 import icon, winmanifest, winresource
from PyInstaller.utils.hooks import collect_all, collect_submodules


# Keep one-file packaging robust on locked-down Windows desktops.
winresource.remove_all_resources = lambda filename: None
winmanifest.write_manifest_to_executable = lambda *args, **kwargs: None
icon.CopyIcons = lambda *args, **kwargs: None

base_dir = os.path.abspath(os.path.join(SPECPATH, ".."))
backend_dir = os.path.join(base_dir, "backend")
app_dir = os.path.join(backend_dir, "app")
app_data_dir = os.path.join(app_dir, "data")
frontend_dir = os.path.join(base_dir, "build", "frontend-dist")
scripts_dir = os.path.join(backend_dir, "scripts")
crypto_datas, crypto_binaries, crypto_hiddenimports = collect_all("cryptography")

datas = []
if os.path.isdir(app_data_dir):
    datas.append((app_data_dir, "app/data"))
if os.path.isdir(frontend_dir):
    datas.append((frontend_dir, "static"))
if os.path.isdir(scripts_dir):
    datas.append((scripts_dir, "scripts"))

a = Analysis(
    [os.path.join(app_dir, "runtime", "exe_launcher.py")],
    pathex=[backend_dir],
    binaries=crypto_binaries,
    datas=datas + crypto_datas,
    hiddenimports=[
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "fastapi",
        "starlette",
        "pydantic",
        "pydantic_settings",
        "sqlalchemy",
        "sqlalchemy.orm",
        "httpx",
        "requests",
        "pymysql",
        "oracledb",
        "psycopg",
        "pymssql",
        "cryptography",
        "app.main",
        "app.api.v1",
        "app.runtime.frontend_server",
        "sqlalchemy.dialects.sqlite",
        "sqlalchemy.dialects.sqlite.pysqlite",
    ]
    + collect_submodules("app")
    + collect_submodules("uvicorn")
    + collect_submodules("fastapi")
    + collect_submodules("starlette")
    + crypto_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="TraceMindAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="NONE",
)
