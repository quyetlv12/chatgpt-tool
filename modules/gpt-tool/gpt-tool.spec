# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


ROOT = Path(SPECPATH)
curl_datas, curl_binaries, curl_hiddenimports = collect_all("curl_cffi")

a = Analysis(
    [str(ROOT / "gpt_tool" / "server.py")],
    pathex=[str(ROOT)],
    binaries=curl_binaries,
    datas=[(str(ROOT / "web"), "web"), *curl_datas],
    hiddenimports=curl_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="GPT-Tool",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    bundle_files = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="GPT-Tool",
    )
    app = BUNDLE(
        bundle_files,
        name="GPT-Tool.app",
        bundle_identifier="com.quyetlv.gpttool",
        info_plist={
            "CFBundleDisplayName": "GPT-Tool",
            "CFBundleName": "GPT-Tool",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="GPT-Tool",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
    )
    windows_bundle = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="GPT-Tool",
    )
