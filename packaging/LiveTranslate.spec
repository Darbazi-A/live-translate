# PyInstaller build of the server with Python and all packages inside, so the target
# Windows computer needs nothing installed.  From the project folder:
#   pip install -r requirements.txt pyinstaller
#   pyinstaller --noconfirm packaging/LiveTranslate.spec
# -> dist/LiveTranslate/LiveTranslate.exe, then packaging/windows/LiveTranslate.iss
# (macOS and Linux run from source with start.sh)
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# web pages (without the test harness) + CA certificates
datas = [
    (os.path.join(ROOT, "public", f), "public")
    for f in os.listdir(os.path.join(ROOT, "public"))
    if os.path.isfile(os.path.join(ROOT, "public", f))
]
datas += collect_data_files("certifi")

a = Analysis(
    [os.path.join(ROOT, "server.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=collect_submodules("edge_tts"),
    excludes=["tkinter", "unittest", "pydoc", "test"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LiveTranslate",
    console=True,   # the console window shows the addresses; closing it stops the server
    icon=os.path.join(SPECPATH, "icon.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="LiveTranslate")

