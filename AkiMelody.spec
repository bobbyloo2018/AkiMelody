# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\gninj\\Downloads\\AKI\\webview_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\gninj\\Downloads\\AKI\\templates', 'templates'), ('C:\\Users\\gninj\\Downloads\\AKI\\static', 'static'), ('C:\\Users\\gninj\\Downloads\\AKI\\build\\qjs.exe', 'build'), ('C:\\Users\\gninj\\Downloads\\AKI\\cookies.txt', '.'), ('C:\\Users\\gninj\\Downloads\\AKI\\headers.json', '.'), ('C:\\Users\\gninj\\AppData\\Local\\Programs\\Python\\Python314\\Lib\\site-packages\\ytmusicapi\\locales', 'ytmusicapi/locales')],
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
    [],
    exclude_binaries=True,
    name='AkiMelody',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\gninj\\Downloads\\AKI\\build\\icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AkiMelody',
)
