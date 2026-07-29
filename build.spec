# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for MomentShift (onedir + bundled ffmpeg).

Run with::

    pyinstaller build.spec

The CI copies the downloaded ffmpeg binaries into ``tools/ffmpeg_bin`` before
building, and this spec bundles them into the dist root (next to the exe), which
is exactly where :mod:`momentshift.core.ffmpeg` looks for them.
"""

import os

APP_NAME = "MomentShift"
# PyInstaller executes this spec via exec() and defines SPECPATH (the directory
# containing the spec file) but does NOT define __file__. Fall back to __file__
# for a direct `python build.spec` run. In CI this resolves to the repo root.
REPO_ROOT = globals().get("SPECPATH") or os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(REPO_ROOT, "src")
FFMPEG_DIR = os.path.join(REPO_ROOT, "tools", "ffmpeg_bin")
SCRIPT = os.path.join(SRC_DIR, "momentshift", "__main__.py")

# Bundle ffmpeg if present (CI places it here before building).
binaries = []
if os.path.isdir(FFMPEG_DIR):
    for fn in os.listdir(FFMPEG_DIR):
        binaries.append((os.path.join(FFMPEG_DIR, fn), "."))

datas = [
    (os.path.join(SRC_DIR, "momentshift", "i18n", "locales"), "momentshift/i18n/locales"),
]

hiddenimports = [
    "momentshift",
    "momentshift.core",
    "momentshift.core.qt_compat",
    "momentshift.core.config",
    "momentshift.core.ffmpeg",
    "momentshift.core.hardware",
    "momentshift.core.presets",
    "momentshift.core.converter",
    "momentshift.core.queue",
    "momentshift.core.models",
    "momentshift.gui",
    "momentshift.gui.base",
    "momentshift.gui.drop_area",
    "momentshift.gui.queue_widget",
    "momentshift.gui.convert_interface",
    "momentshift.gui.setting_interface",
    "momentshift.gui.about_interface",
    "momentshift.gui.main_window",
    "momentshift.i18n",
    "momentshift.i18n.translator",
]

a = Analysis(
    [SCRIPT],
    pathex=[SRC_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name=APP_NAME,
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
    name=APP_NAME,
)
