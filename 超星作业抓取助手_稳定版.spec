# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


excluded_modules = [
    'PySide6',
    'PyQt5',
    'PyQt6',
    'qtpy',
    'tkinter',
    '_tkinter',
    'numpy',
    'cv2',
    'IPython',
    'jupyter',
    'jupyter_client',
    'matplotlib',
    'pandas',
    'scipy',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('app.html', '.'),
        ('logo.png', '.'),
        ('logo.ico', '.'),
    ],
    hiddenimports=[
        'backend',
        'chaoxing_auto',
        'chaoxing_batch',
        'chaoxing_scraper',
        'webview.platforms.winforms',
        'webview.platforms.edgechromium',
        'clr',
        'pythonnet',
        'bs4',
        'requests',
        'DrissionPage',
        'openpyxl',
        'psutil',
        'docx',
        'docx.enum.text',
        'docx.oxml',
        'docx.oxml.ns',
        'docx.shared',
        'PIL',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
    optimize=0,
)

# A stray Python 3.8 runtime exists beside the active Python 3.10 runtime on
# this machine. It is not used by the app and must not be redistributed.
a.binaries = [
    item for item in a.binaries
    if Path(item[0]).name.lower() != 'python38.dll'
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='超星作业抓取助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='logo.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='超星作业抓取助手稳定版',
)
