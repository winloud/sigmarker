# -*- mode: python ; coding: utf-8 -*-

import os
import sys
import runpy
import shutil
import subprocess
import json
from pathlib import Path
from importlib.metadata import distribution
from PyInstaller.utils.win32.versioninfo import VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct

project_root = Path(SPECPATH)
metadata = runpy.run_path(str(project_root / 'sigmarker' / 'metadata.py'))
product_version = metadata['APP_VERSION']
version_tuple = tuple(map(int, product_version.split('.'))) + (0,)
build_info = {'version': product_version, 'commit': None, 'dirty': None}
if shutil.which('git') and (project_root / '.git').exists():
    build_info['commit'] = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=project_root, text=True).strip()
    build_info['dirty'] = bool(subprocess.check_output(
        ['git', 'status', '--porcelain'], cwd=project_root, text=True).strip())

# pi-heif 的 Windows wheel 把 DLL 放在 site-packages 根目录而非包内。
# 只收集该发行包登记的库，避免扫描并带入其他工具的 DLL。
heif_binaries = []
heif_distribution = distribution('pi-heif')
for relative in heif_distribution.files or ():
    if relative.suffix.lower() == '.dll':
        heif_binaries.append((str(heif_distribution.locate_file(relative)), '.'))

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
    binaries=heif_binaries,
    datas=[(str(Path(SPECPATH) / 'sigmarker' / 'assets' / name), 'sigmarker/assets')
           for name in ('synthetic.heic', 'sigmarker.ico', 'chevron-down.svg',
                        'rotate-ccw.svg', 'rotate-cw.svg', 'auto-crop.svg',
                        'close.svg', 'folder.svg', 'phone.svg')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'IPython', 'pytest', 'notebook', 'jupyter', 'pandas', 'pandas.libs'],
    noarchive=False,
    optimize=0,
)

# QtGui's generic image-reader hook also brings in QtPdf. This app reads PDF
# through MuPDF, so the optional Qt PDF image reader is outside its runtime.
a.binaries = [item for item in a.binaries
              if Path(item[0]).name.lower() not in {'qpdf.dll', 'qt6pdf.dll'}]

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
    version=VSVersionInfo(
        ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple,
                         mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)),
        kids=[StringFileInfo([StringTable('040904B0', [
            StringStruct('CompanyName', metadata['APP_AUTHOR']),
            StringStruct('FileDescription', 'SigMarker signature extraction tool'),
            StringStruct('FileVersion', product_version),
            StringStruct('ProductName', metadata['APP_NAME']),
            StringStruct('ProductVersion', product_version),
            StringStruct('OriginalFilename', 'SigMarker.exe'),
            StringStruct('LegalCopyright', 'Copyright (C) 2026 winloud; AGPL-3.0-only'),
        ])]), VarFileInfo([VarStruct('Translation', [0x0409, 1200])])]),
    icon=str(Path(SPECPATH) / 'sigmarker' / 'assets' / 'sigmarker.ico'),
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

# Legal documents sit next to the executable so they remain readable without Qt.
for document in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'SOURCE_CODE.md',
                 'CHANGELOG.md', 'requirements-lock.txt', 'third_party_sources.json'):
    shutil.copy2(project_root / document, Path(coll.name) / document)
shutil.copytree(project_root / 'licenses', Path(coll.name) / 'licenses', dirs_exist_ok=True)
(Path(coll.name) / 'build-info.json').write_text(
    json.dumps(build_info, indent=2) + '\n', encoding='utf-8')
