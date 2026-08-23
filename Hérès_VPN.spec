# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path


project_root = Path(SPECPATH).resolve()
sys.path.insert(0, str(project_root))

from release_bundle import audit_release_inputs, audited_datas


audit_release_inputs(project_root)

a = Analysis(
    [str(project_root / 'vpn-gui-app' / 'app.py')],
    pathex=[str(project_root / 'vpn-gui-app')],
    binaries=[],
    datas=audited_datas(project_root),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', '_pytest', 'ruff', 'mypy', 'coverage', 'hypothesis', 'tests'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Hérès_VPN',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(project_root / 'vpn-gui-app' / 'ui' / 'assets' / 'Hérès_VPN_logo.ico')],
)
