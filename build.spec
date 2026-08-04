# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build specification for MomentShift (onedir + bundled ffmpeg).

Run with::

    pyinstaller build.spec

The CI copies the downloaded ffmpeg binaries into ``tools/ffmpeg_bin`` before
building, and this spec bundles them into the dist root (next to the exe), which
is exactly where :mod:`momentshift.core.ffmpeg` looks for them.
"""

import os
import sys

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
    (os.path.join(SRC_DIR, "momentshift", "resources", "icons"), "momentshift/resources/icons"),
    (os.path.join(SRC_DIR, "momentshift", "i18n", "locales"), "momentshift/i18n/locales"),
    # v0.8.1：随包字体（load_app_fonts 从 src/momentshift/resources/ 查找）
    (os.path.join(SRC_DIR, "momentshift", "resources", "HarmonyOS_Sans_SC_Regular.ttf"), "momentshift/resources"),
    (os.path.join(SRC_DIR, "momentshift", "resources", "FiraCode-Regular.ttf"), "momentshift/resources"),
]

# 压缩工具二进制：Windows 随包 ``*.exe``；Linux / macOS 若有同名无后缀二进制也一并打包。
# 仅当文件实际存在才打包（不同平台构建时按平台放入对应二进制，缺失则跳过，不报错）。
for _stem in ("oxipng", "jpegoptim", "gifsicle"):
    _fname = f"{_stem}.exe" if sys.platform == "win32" else _stem
    _src = os.path.join(SRC_DIR, "momentshift", "resources", _fname)
    if os.path.isfile(_src):
        datas.append((_src, "momentshift/resources"))

hiddenimports = [
    "momentshift",
    "momentshift.core",
    "momentshift.core.qt_compat",
    "momentshift.core.config",
    "momentshift.core.ffmpeg",
    "momentshift.core.hardware",
    "momentshift.core.presets",
    "momentshift.core.advanced",
    "momentshift.core.converter",
    "momentshift.core.queue",
    "momentshift.core.models",
    "momentshift.core.ffmpeg_download",
    "momentshift.core.logger",
    "momentshift.core.compressor",
    "momentshift.core.engines",
    "momentshift.core.engine_download",
    "momentshift.core.upscaler",
    "momentshift.core.tools_download",
    "momentshift.core.asr_client",
    "momentshift.core.asr_worker",
    "momentshift.core.funasr_engine",
    "momentshift.core.funasr_download",
    "momentshift.core.funasr",
    "momentshift.core.funasr.paraformer_bin",
    "momentshift.core.funasr.sensevoice_bin",  # v0.8.5 结构化输出：SenseVoiceSmall（引擎内延迟 import，显式声明）
    "momentshift.core.funasr.vad_bin",         # v0.8.5 结构化输出：FSMN-VAD
    "momentshift.core.funasr.spk_bin",         # v0.8.5 结构化输出：CAM++ 说话人嵌入
    "momentshift.core.funasr.utils",
    "momentshift.core.funasr.utils.frontend",
    "momentshift.core.funasr.utils.utils",
    "momentshift.core.funasr.utils.postprocess_utils",
    "momentshift.core.funasr.utils.yaml_light",
    "momentshift.core.funasr.utils.wav_io",
    "momentshift.core.funasr.utils.e2e_vad",   # v0.8.5：VAD 后处理（纯 numpy 复刻）
    "momentshift.core.funasr.utils.sentencepiece_decode",  # v0.8.5：纯 Python BPE 解码（tokens.json）
    "onnxruntime",          # v0.8.4 内置 FunASR：引擎在函数内延迟 import，显式声明防漏
    "onnxruntime.capi",
    "jieba",
    # v0.8.7：PyInstaller 打包后 urllib.urlopen 报 "unknown url type: https" 的
    # 经典根因是 urllib/ssl/http.client 收集不全（用户实测网络正常却下载失败）。
    # 显式收集整套 HTTP(S) 栈。
    "urllib.request",
    "urllib.error",
    "urllib.parse",
    "http.client",
    "ssl",
    "email",
    "email.mime",
    "email.mime.multipart",
    "PIL",
    "PIL.Image",
    "momentshift.gui",
    "momentshift.gui.ffmpeg_card",
    "momentshift.gui.format_grid",
    "momentshift.gui.advanced_panel",
    "momentshift.gui.base",
    "momentshift.gui.drop_area",
    "momentshift.gui.queue_widget",
    "momentshift.gui.compress_interface",
    "momentshift.gui.convert_interface",
    "momentshift.gui.convert_setup_dialog",
    "momentshift.gui.upscale_interface",
    "momentshift.gui.compare_widget",
    "momentshift.gui.engine_card",
    "momentshift.gui.setting_interface",
    "momentshift.gui.about_interface",
    "momentshift.gui.main_window",
    "momentshift.gui.asr_interface",  # v0.8.5：main_window 用 importlib 动态 import 界面，PyInstaller 静态分析看不到，必须显式 hiddenimports（v0.8.3/0.8.4 漏列导致 ASR 组件在构建产物中缺失）
    "PyQt6.QtNetwork",   # v0.7.16：QLocalServer/QLocalSocket 单实例 IPC
    "momentshift.gui.theme",
    "momentshift.gui.quick_launch_interface",
    "momentshift.core.quick_launch",
    "momentshift.gui.quick_dialogs",
    "momentshift.quick_runner",
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
    # UPX is disabled on purpose: it packs the PyQt6 DLLs, which makes Windows
    # Defender re-scan (and the OS re-decompress in memory) on every launch —
    # a measurable hit to cold-start time. Uncompressed binaries load directly
    # and are treated as known-good by AV (v0.2.6, #2).
    upx=False,
    name=APP_NAME,
)
