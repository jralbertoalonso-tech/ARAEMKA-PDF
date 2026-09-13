# -*- mode: python ; coding: utf-8 -*-
#
# Archivo de especificación para PyInstaller.
# Se utiliza tanto en macOS (genera ARAEMKA PDF.app) como en Windows
# (genera ARAEMKA-PDF.exe). En CI se invoca con:
#     pyinstaller ARAEMKA-PDF.spec
#
# Construye un único ejecutable portable (--onefile, sin consola).
#
# La opción `collect_all('tkinterdnd2')` es esencial: tkinterdnd2 trae
# binarios nativos (la librería TkDnD) que PyInstaller no detecta por
# análisis estático.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datos_dnd, binarios_dnd, ocultos_dnd = collect_all("tkinterdnd2")

block_cipher = None

# Iconos de la aplicación (si faltan, se empaqueta sin icono sin fallar)
ICONO_MAC = "recursos/araemka-pdf.icns"
ICONO_WIN = "recursos/araemka-pdf.ico"
icono_mac = ICONO_MAC if Path(ICONO_MAC).exists() else None
icono_win = ICONO_WIN if Path(ICONO_WIN).exists() else None


a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=binarios_dnd,
    datas=datos_dnd,
    hiddenimports=ocultos_dnd + [
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Excluimos paquetes pesados que ningún módulo usa. Desde la v1.2,
        # numpy y OpenCV (headless) SÍ van incluidos: los necesita el motor
        # pdf2docx de la pestaña "PDF a Word", y por eso el binario es
        # notablemente más grande que en la v1.0/1.1.
        "scipy", "pandas", "matplotlib",
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "test", "tests",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ARAEMKA-PDF",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,             # Comprime el binario si UPX está disponible.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # --windowed / --noconsole
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icono_win,       # icono del .exe (ignorado fuera de Windows)
)

# En macOS produce además un bundle .app
app = BUNDLE(
    exe,
    name="ARAEMKA PDF.app",
    icon=icono_mac,
    bundle_identifier="com.araemka.pdf",
    info_plist={
        "CFBundleName": "ARAEMKA PDF",
        "CFBundleDisplayName": "ARAEMKA PDF",
        "CFBundleVersion": "1.2.0",
        "CFBundleShortVersionString": "1.2.0",
        "NSHighResolutionCapable": "True",
        "LSMinimumSystemVersion": "11.0",
    },
)
