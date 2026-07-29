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
SCRIPT = os.path.join(SRC_DIR, "momentshift", "__main__.py")

# NOTE: ffmpeg is intentionally NOT bundled — it would bloat the installer.
# Users download it themselves (or via the in-app one-click button) and place
# ffmpeg.exe / ffprobe.exe next to the executable. See FfmpegCard.
binaries = []

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
    "momentshift.core.ffmpeg_download",
    "momentshift.core.logger",
    "momentshift.gui",
    "momentshift.gui.ffmpeg_card",
    "momentshift.gui.format_dialog",
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
