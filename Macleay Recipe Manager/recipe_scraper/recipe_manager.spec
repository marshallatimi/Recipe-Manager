# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Macleay Recipe Manager
# Build locally:
#   python generate_icon.py
#   python create_version_info.py 1.0.0
#   pyinstaller recipe_manager.spec

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
import os

# ── Data files to bundle ──────────────────────────────────────────────────────
datas = [
    ('static', 'static'),
    ('_version.py', '.'),        # version string read at runtime
]

# recipe_scrapers and its full dependency tree ship data files
datas += collect_data_files('recipe_scrapers')
datas += collect_data_files('extruct',  include_py_files=False)
datas += collect_data_files('mf2py')          # backcompat-rules file (fixes crash)
datas += collect_data_files('w3lib')
datas += collect_data_files('parsel')

# xhtml2pdf ships data files (PDF fonts etc.)
try:
    datas += collect_data_files('xhtml2pdf')
except Exception:
    pass
try:
    datas += collect_data_files('reportlab')
except Exception:
    pass

# ── Hidden imports ────────────────────────────────────────────────────────────
hiddenimports = (
    collect_submodules('recipe_scrapers') +
    collect_submodules('flask') +
    collect_submodules('werkzeug') +
    collect_submodules('jinja2') +
    collect_submodules('webview') +
    collect_submodules('mf2py') +
    collect_submodules('xhtml2pdf') +
    collect_submodules('reportlab') +
    collect_submodules('pypdf') +
    [
        'sqlite3',
        'json',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'encodings.utf_8',
        'encodings.ascii',
        'urllib.request',
        'threading',
        'xhtml2pdf',
        'xhtml2pdf.pisa',
        'reportlab',
        'reportlab.pdfgen',
        'reportlab.lib',
        'html5lib',
    ]
)

# ── Icon & version info ───────────────────────────────────────────────────────
_icon    = 'icon.ico'              if os.path.exists('icon.ico')              else None
_verfile = 'file_version_info.txt' if os.path.exists('file_version_info.txt') else None

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ['launcher.py'],
    pathex=[os.path.abspath('.')],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'test', 'unittest'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='RecipeManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon=_icon,
    version=_verfile,
)
