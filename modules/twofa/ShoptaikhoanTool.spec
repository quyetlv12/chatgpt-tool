# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import shutil

from PyInstaller.utils.hooks import collect_all


ROOT = Path(SPECPATH).resolve()
SOURCE_DIR = ROOT / "change 2fa community"
ICON = ROOT / "packaging" / "macos" / "AppIcon.icns"
MENU_BAR_HELPER = ROOT / "build" / "macos" / "ShoptaikhoanMenuBar"

node_path = shutil.which("node")
if not node_path:
    for candidate in (
        "/opt/homebrew/opt/node@24/bin/node",
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
    ):
        if Path(candidate).is_file():
            node_path = candidate
            break
if not node_path:
    raise SystemExit("Node.js is required to build the macOS app")
if not MENU_BAR_HELPER.is_file():
    raise SystemExit("Native macOS menu-bar helper must be built first")

curl_datas, curl_binaries, curl_hiddenimports = collect_all("curl_cffi")

a = Analysis(
    [str(SOURCE_DIR / "server.py")],
    pathex=[str(ROOT), str(SOURCE_DIR)],
    binaries=[(node_path, "."), (str(MENU_BAR_HELPER), "."), *curl_binaries],
    datas=[
        (str(SOURCE_DIR / "static"), "static"),
        (str(ROOT / "openai_sentinel_quickjs.js"), "."),
        *curl_datas,
    ],
    hiddenimports=[
        *curl_hiddenimports,
        "request_phase",
        "macos_integration",
        "sentinel_pow",
        "sentinel_quickjs",
        "mfa_phase",
        "session_phase",
        "user_agent_profile",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["playwright", "camoufox"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Shoptaikhoan Tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Shoptaikhoan Tool",
)
app = BUNDLE(
    coll,
    name="Shoptaikhoan Tool.app",
    icon=str(ICON),
    bundle_identifier="com.shoptaikhoan.tool.change2fa",
    version="1.0.0",
    info_plist={
        "CFBundleDisplayName": "Shoptaikhoan Tool",
        "CFBundleName": "Shoptaikhoan Tool",
        "LSMinimumSystemVersion": "13.5",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Shoptaikhoan Tool",
        "LSUIElement": True,
        "LSMultipleInstancesProhibited": True,
    },
)
