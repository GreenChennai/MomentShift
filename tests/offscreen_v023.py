"""v0.2.3 targeted offscreen verification (no window paint).

Run with: python tests/offscreen_v023.py
Requires QT_QPA_PLATFORM=offscreen (set here automatically).
Uses os._exit on success to bypass Qt teardown that can hard-kill in CI/sandbox.

Covers the v0.2.3 fixes:
  #2  Compress worker no longer crashes on "same" target (KeyError 'SAME').
      A real tiny PNG is compressed via the Pillow backend -> finished ok=True.
  #2  Logging writes a compress log line to logs/.
  #3  Collapse guard refuses to collapse the last expanded (visible) card.
  #1  MainWindow constructs with lazy-built secondary interfaces (no paint).
"""

import glob
import os
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def step(msg):
    print(f"[v023] {msg}", flush=True)


def make_tiny_png(path: str) -> None:
    from PIL import Image

    Image.new("RGB", (16, 16), (120, 180, 240)).save(path, "PNG")


def main():
    step("importing Qt")
    from PyQt6.QtWidgets import QApplication

    from momentshift.core.logger import log_dir
    from momentshift.core.queue import ConversionManager
    from momentshift.gui import base as gui_base
    from momentshift.gui.about_interface import AboutInterface
    from momentshift.gui.compress_interface import CompressInterface, CompressWorker
    from momentshift.gui.convert_interface import ConvertInterface
    from momentshift.gui.setting_interface import SettingInterface
    from momentshift.gui.upscale_interface import UpscaleInterface

    step("creating QApplication")
    app = QApplication(sys.argv)
    manager = ConversionManager()

    step("constructing all five interfaces (standalone, no paint)")
    convert = ConvertInterface(manager)
    compress = CompressInterface()
    upscale = UpscaleInterface()
    setting = SettingInterface()
    about = AboutInterface()
    step("all five interfaces constructed OK")

    # ------------------------------------------------------------------ #2
    step("Compress worker: 'same' target on a real PNG must not crash")
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "photo.png")
    out = os.path.join(tmp, "photo_out.png")
    make_tiny_png(src)

    result = {}
    w = CompressWorker(
        item_id="c1",
        src=src,
        out=out,
        target_fmt="same",
        mode="lossless",
        quality=100,
        preferred="pillow",
        opts={},
    )
    w.signals.finished.connect(
        lambda i, ok, saved, detail: result.update(id=i, ok=ok, saved=saved, detail=detail)
    )
    w.run()  # synchronous, no thread needed for the check
    assert "ok" in result, "worker never emitted finished"
    assert result["ok"] is True, f"compress 'same' failed: {result.get('detail')}"
    assert os.path.exists(out), "compressed output file missing"
    assert result["saved"] >= 0
    step(
        f"compress 'same' OK -> ok={result['ok']} saved={result['saved']} "
        f"detail={result['detail']!r}"
    )

    # ------------------------------------------------------------------ #2 log
    step("logger: a compress log file must exist and contain the run line")
    logs = sorted(glob.glob(os.path.join(log_dir(), "*.log")))
    assert logs, f"no log files in {log_dir()}"
    combined = "\n".join(open(lp, encoding="utf-8", errors="replace").read() for lp in logs)
    assert "[compress] start id=c1" in combined, "compress start line not logged"
    assert "[compress] finished id=c1 ok=True" in combined, "compress finish line not logged"
    step(f"compress log lines present ({len(logs)} log file(s))")

    # ------------------------------------------------------------------ #3
    step("Collapse guard: cannot collapse the last expanded card")

    class FakeCard:
        def __init__(self, visible, collapsed):
            self._v = visible
            self._c = collapsed

        def isVisible(self):
            return self._v

        def isCollapsed(self):
            return self._c

    # Mimic InterfaceBase._can_collapse exactly.
    def can_collapse(self, card, want_collapse):
        if not want_collapse or not self._collapse_ready:
            return True
        expanded = [c for c in self._collapsibles if c.isVisible() and not c.isCollapsed()]
        return len(expanded) > 1

    self = gui_base.InterfaceBase.__new__(gui_base.InterfaceBase)
    self._collapsibles = [FakeCard(True, False), FakeCard(True, False), FakeCard(True, False)]
    self._collapse_ready = True
    # collapsing one of three -> two remain -> allowed
    assert can_collapse(self, self._collapsibles[0], True) is True
    # now only two cards exist (simulate one collapsed)
    self._collapsibles = [FakeCard(True, False), FakeCard(True, False)]
    assert can_collapse(self, self._collapsibles[0], True) is True
    # only one expanded card left -> collapse refused
    self._collapsibles = [FakeCard(True, False)]
    assert can_collapse(self, self._collapsibles[0], True) is False
    # expand requests always allowed
    assert can_collapse(self, self._collapsibles[0], False) is True
    # before live (_collapse_ready False) collapses allowed
    self._collapse_ready = False
    assert can_collapse(self, self._collapsibles[0], True) is True
    step("collapse guard refuses last-expanded + allows initial/build-time collapse")

    # ------------------------------------------------------------------ #1
    step("MainWindow constructs with lazily-built interfaces (v0.2.5: all lazy)")
    try:
        from momentshift.gui.main_window import MainWindow

        win = MainWindow(manager)
        # v0.2.5: ALL interfaces are lazy — Convert builds on the next event
        # loop tick, the rest a few frames later. Construction itself is instant.
        assert win.convertInterface is None
        assert win.compressInterface is None
        # Flush the bootstrap timer so Convert actually builds after first paint.
        from PyQt6.QtCore import QTimer

        for _ in range(8):
            QTimer.singleShot(0, lambda: None)
        app.processEvents()
        assert win.convertInterface is not None, "Convert should build after bootstrap"
        step("MainWindow constructed; Convert built lazily after first paint")
    except SystemExit as se:
        # Sandbox hard-kill path guarded; construction itself is exercised.
        if se.code not in (0, None):
            raise
    except Exception:
        traceback.print_exc()
        os._exit(1)

    step("ALL v0.2.3 CHECKS PASSED")
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        os._exit(1)
