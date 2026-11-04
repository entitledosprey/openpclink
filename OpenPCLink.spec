# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['openpclink.cli', 'openpclink.gui', 'openpclink.link_helper', 'serial.tools.list_ports', 'PySide6.QtMultimedia']
hiddenimports += collect_submodules('pyftdi')
hiddenimports += collect_submodules('usb')


a = Analysis(
    ['packaging/entry.py'],
    pathex=['src'],
    binaries=[('/opt/homebrew/opt/libusb/lib/libusb-1.0.0.dylib', '.')],
    datas=[('src/openpclink/data', 'openpclink/data')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['unicorn', 'pefile', 'openpclink.rules', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.Qt3DCore', 'PySide6.QtPdf', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OpenPCLink',
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OpenPCLink',
)
app = BUNDLE(
    coll,
    name='OpenPCLink.app',
    icon=None,
    bundle_identifier='org.openpclink.app',
)
