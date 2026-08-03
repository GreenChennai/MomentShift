"""Headless validation of the convert-interface flow (no InfoBar paint).

InfoBar is patched to a no-op so the offscreen sandbox doesn't hard-kill on the
toast paint. We validate: staging construction, format-matrix rebuild, advanced
panel rebuild, add-to-queue (per-file), and add-to-queue with merge enabled.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from unittest.mock import patch

from PyQt6.QtWidgets import QApplication
from qfluentwidgets import InfoBar

# Neutralise toasts (their paint kills the offscreen sandbox).
InfoBar.success = staticmethod(lambda *a, **k: None)
InfoBar.warning = staticmethod(lambda *a, **k: None)
InfoBar.error = staticmethod(lambda *a, **k: None)

from momentshift.core import advanced
from momentshift.core.config import cfg
from momentshift.core.queue import ConversionManager
from momentshift.gui.convert_interface import ConvertInterface


def check(cond, msg):
    print(("PASS" if cond else "FAIL"), msg)
    if not cond:
        raise SystemExit(1)


app = QApplication([])
mgr = ConversionManager(ffmpeg_path=None)
conv = ConvertInterface(mgr, None)
conv.show = lambda *a, **k: None  # guard against accidental paint

d = tempfile.mkdtemp()
cfg.outputMode.value = "same"

# --- per-file image flow ------------------------------------------------
f1 = os.path.join(d, "a.png")
f2 = os.path.join(d, "b.png")
open(f1, "wb").write(b"x")
open(f2, "wb").write(b"y")
conv._add_to_staging([f1, f2])
check(len(conv._staged) == 2, "two files staged")
check("image" in conv.advancedPanel._cat_panels, "advanced panel shows image sub-panel")
conv._format_by_cat["image"] = "jpg"
conv._refresh_format_cards()
conv._on_add_to_queue()
check(len(mgr.tasks) == 2, "two image tasks enqueued")
check(all(t.target_format == "jpg" for t in mgr.tasks), "image tasks target jpg")

# --- merge video flow ---------------------------------------------------
mgr.clear()
conv._staged.clear()
conv._refresh_staging()
advanced.reset()
advanced.adv["video"]["merge"] = True
v1 = os.path.join(d, "v1.mp4")
v2 = os.path.join(d, "v2.mp4")
open(v1, "wb").write(b"x")
open(v2, "wb").write(b"y")
conv._add_to_staging([v1, v2])
conv._format_by_cat["video"] = "mp4"
conv._refresh_format_cards()
check("video" in conv.advancedPanel._cat_panels, "advanced panel shows video sub-panel")
conv._on_add_to_queue()
check(len(mgr.tasks) == 1 and mgr.tasks[0].merge, "merge creates ONE task")
check(len(mgr.tasks[0].input_paths) == 2, "merge task has 2 inputs")

print("ALL GUI-FLOW CHECKS PASSED")
sys.exit(0)
