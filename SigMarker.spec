# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

# 外部工具的 PATH 可能带有同名但 ABI 不兼容的 ICU/UCRT DLL。
# 只影响本次构建进程, 不修改用户的系统环境变量。
if sys.platform == 'win32':
    system_root = Path(os.environ['SystemRoot'])
    os.environ['PATH'] = os.pathsep.join(map(str, (
        system_root / 'System32', system_root,
        Path(sys.base_prefix), Path(sys.base_prefix) / 'Scripts',
    )))

a = Analysis(
    ['main.py'],
    pathex=[SPECPATH],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'IPython', 'pytest', 'notebook', 'jupyter', 'pandas', 'pandas.libs'],
    noarchive=False,
    optimize=0,
)

if sys.platform == 'win32':
    for destination, source, _ in a.binaries:
        if Path(destination).name.lower() in {'icuuc.dll', 'ucrtbase.dll'}:
            if not Path(source).resolve().is_relative_to(system_root.resolve()):
                raise RuntimeError(f'Unexpected system DLL source: {destination} <- {source}')

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SigMarker',
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
    name='SigMarker',
)
