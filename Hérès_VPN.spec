# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['vpn-gui-app\\app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('vpn-gui-app\\ui', 'vpn-gui-app/ui'),
        ('vpn-gui-app\\provider_catalog.json', 'vpn-gui-app'),
        ('vpn-scaleway', 'vpn-scaleway'),
        ('vpn-digitalocean', 'vpn-digitalocean'),
        ('vpn-aws-lightsail', 'vpn-aws-lightsail'),
        ('terraform-common', 'terraform-common'),
    ],
    hiddenimports=[],
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
    icon=['vpn-gui-app\\ui\\assets\\Hérès_VPN_logo.ico'],
)
