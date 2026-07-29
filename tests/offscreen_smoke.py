"""Offscreen GUI construction + interaction smoke test.

Run with: python tests/offscreen_smoke.py
Requires QT_QPA_PLATFORM=offscreen (set here automatically).
Uses os._exit on success to bypass Qt teardown that can hard-kill in CI/sandbox.
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import traceback


def step(msg):
    print(f"[step] {msg}", flush=True)


def main():
    step("importing Qt + app")
    from PyQt6.QtWidgets import QApplication

    step("importing MainWindow")
    from momentshift.gui.main_window import MainWindow
    step("importing ConversionManager")
    from momentshift.core.queue import ConversionManager
    step("importing translator/config")
    from momentshift.i18n.translator import tr, translator
    from momentshift.core.config import cfg

    step("creating QApplication")
    app = QApplication(sys.argv)

    step("creating ConversionManager")
    manager = ConversionManager()
    step("creating MainWindow")
    window = MainWindow(manager)
    window.show()
    step("window constructed")

    step("switch language -> en_US")
    cfg.language.value = "en_US"
    assert tr("app.title") == "MomentShift", tr("app.title")

    step("switch language -> zh_CN")
    cfg.language.value = "zh_CN"
    assert tr("app.title") == "瞬变工坊", tr("app.title")

    step("switch language -> zh_TW")
    cfg.language.value = "zh_TW"
    assert tr("app.title") == "瞬變工坊", tr("app.title")

    step("switch theme -> dark")
    cfg.theme.value = "dark"
    assert cfg.theme.value == "dark"

    step("unsupported file rejected")
    convert = window.convertInterface
    tmp = tempfile.mkdtemp()
    bad = os.path.join(tmp, "secret_file.xyz")
    with open(bad, "wb") as fh:
        fh.write(b"nope")
    convert._on_paths([bad])
    assert manager.tasks == [], "unsupported file should be rejected"
    assert len(convert.queueList.items) == 0

    step("ffmpeg card present + status consistent")
    from momentshift.core.ffmpeg import find_ffmpeg
    assert hasattr(convert, "ffmpegCard"), "FfmpegCard missing on start screen"
    assert convert.ffmpegCard.downloadBtn is not None
    assert convert.ffmpegCard.linkBtn is not None
    # manager.has_ffmpeg must agree with a fresh find_ffmpeg() probe
    assert manager.has_ffmpeg == (find_ffmpeg(cfg.ffmpegSource.value) is not None)

    step("ALL CHECKS PASSED")
    print(f"final locale: {translator.locale.value}", flush=True)
    print(f"has_ffmpeg: {manager.has_ffmpeg}", flush=True)
    # Bypass Qt teardown (offscreen/sandbox can hard-kill on exit).
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        os._exit(1)
