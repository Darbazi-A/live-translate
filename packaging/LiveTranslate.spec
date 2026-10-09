# PyInstaller build of the server with Python and all packages inside, so the target
# computer needs nothing installed.  From the project folder:
#   pip install -r requirements.txt pyinstaller
#   pyinstaller --noconfirm packaging/LiveTranslate.spec
# Windows -> dist/LiveTranslate/LiveTranslate.exe (then packaging/windows/LiveTranslate.iss)
# macOS   -> dist/Live Translate.app
import os
import re
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
VERSION = re.search(r'^VERSION = "(.+)"', open(os.path.join(ROOT, "server.py"), encoding="utf-8").read(), re.M).group(1)
MAC = sys.platform == "darwin"

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
    console=not MAC,   # Windows: the console window shows the addresses; closing it stops the server
    icon=os.path.join(SPECPATH, "icon.icns" if MAC else "icon.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="LiveTranslate")

if MAC:
    app = BUNDLE(
        coll,
        name="Live Translate.app",
        icon=os.path.join(SPECPATH, "icon.icns"),
        bundle_identifier="ge.darbazi.livetranslate",
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            # background app: no Dock icon; it opens the speaker page in Chrome and is
            # stopped with the "turn off server" button there
            "LSUIElement": True,
            "NSLocalNetworkUsageDescription": "Phones on the same Wi-Fi connect to this computer to receive the live translation.",
        },
    )
