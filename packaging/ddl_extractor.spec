# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: genera dist/ddl-extractor/ con
#   ddl-extractor(.exe)      -> GUI (sin consola)
#   ddl-extractor-cli(.exe)  -> CLI (validate / extract)
# Uso: pyinstaller --noconfirm --clean packaging/ddl_extractor.spec
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
ICON = str(ROOT / "assets" / "icon.ico") if sys.platform == "win32" else None
DATAS = [(str(ROOT / "docs"), "docs"), (str(ROOT / "assets"), "assets")]
HIDDEN = ["psycopg2", "psycopg2.extensions", "ddl_common", "validate_ddl", "extract_ddl"]
# Modulos Qt que no se usan: reducen ~150 MB
EXCLUDES = [
    "tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtWebSockets", "PySide6.QtWebChannel", "PySide6.QtNetworkAuth",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtSvgWidgets",
]

gui = Analysis([str(ROOT / "run_gui.py")], pathex=[str(ROOT)], datas=DATAS,
               hiddenimports=HIDDEN, excludes=EXCLUDES)
cli = Analysis([str(ROOT / "cli.py")], pathex=[str(ROOT)], datas=[],
               hiddenimports=HIDDEN, excludes=EXCLUDES + ["PySide6", "shiboken6"])

gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="ddl-extractor",
              console=False, icon=ICON, upx=False)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="ddl-extractor-cli",
              console=True, icon=ICON, upx=False)

coll = COLLECT(gui_exe, gui.binaries, gui.datas, cli_exe, cli.binaries, cli.datas,
               name="ddl-extractor", upx=False)
