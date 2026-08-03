"""Engine + component checks for the advanced-settings / format-matrix rework.

Kept UI-free where possible: we never show a populated queue row (the offscreen
sandbox hard-kills that paint). We validate the ffmpeg-arg building, the merge
task creation, the format-matrix selection, and the advanced panel construction.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PyQt6.QtWidgets import QApplication

from momentshift.core import advanced
from momentshift.core.models import Task
from momentshift.core.presets import build_args
from momentshift.core.queue import ConversionManager
from momentshift.gui.advanced_panel import AdvancedPanel
from momentshift.gui.format_grid import FormatGrid


def check(cond, msg):
    print(("PASS" if cond else "FAIL"), msg)
    if not cond:
        raise SystemExit(1)


app = QApplication([])

# 1) merge command (video) ------------------------------------------------
advanced.reset()
advanced.adv["video"]["merge"] = True
t = Task(
    id="x",
    input_path="/a/1.mp4",
    output_path="/a/out.mp4",
    target_format="mp4",
    category="video",
    use_gpu=False,
    merge=True,
    input_paths=["/a/1.mp4", "/a/2.mp4"],
)
args = build_args(t, {})
check(
    any("filter_complex" in a for a in args) and any("concat=n=2" in a for a in args),
    "merge video uses concat filter",
)
check(args[-1] == "/a/out.mp4", "merge output path is last arg")

# 2) advanced video params ------------------------------------------------
advanced.reset()
advanced.adv["video"]["resolution"] = "1280x720"
advanced.adv["video"]["fps"] = "30"
advanced.adv["video"]["bitrate"] = "5M"
t2 = Task(
    id="y",
    input_path="/a/1.mp4",
    output_path="/a/out.mp4",
    target_format="mp4",
    category="video",
    use_gpu=False,
)
args2 = build_args(t2, {})
check(
    any("scale=1280:720" in a and "fps=30" in a for a in args2),
    "video resolution + fps applied via -vf",
)
check(any(a == "-b:v" for a in args2) and any("5M" in a for a in args2), "video bitrate applied")

# 3) image quality (lossy jpg) --------------------------------------------
advanced.reset()
advanced.adv["image"]["quality"] = 50
t3 = Task(
    id="z",
    input_path="/a/1.png",
    output_path="/a/out.jpg",
    target_format="jpg",
    category="image",
    use_gpu=False,
)
args3 = build_args(t3, {})
check("-q:v" in args3, "jpg quality mapped to -q:v")

# 4) manager merge add ----------------------------------------------------
mgr = ConversionManager(ffmpeg_path=None)
d = tempfile.mkdtemp()
p1 = os.path.join(d, "1.mp4")
p2 = os.path.join(d, "2.mp4")
open(p1, "wb").write(b"123")
open(p2, "wb").write(b"456")
advanced.reset()
advanced.adv["video"]["merge"] = True
added, skipped = mgr.add_files([p1, p2], "mp4", None, False, output_mode="same", suffix="_c")
check(len(added) == 1 and added[0].merge, "manager creates ONE merge task")
check(len(added[0].input_paths) == 2, "merge task stores both inputs")

# 5) format matrix -------------------------------------------------------
g = FormatGrid()
g.setup(["image", "video"], {"image": "png", "video": "mp4"})
check(len(g._cards.get("image", [])) > 0, "format grid built for image")
g._on_card("image", "webp")
check(g.get_selection()["image"] == "webp", "format grid selection updates on click")

# 6) advanced panel ------------------------------------------------------
ap = AdvancedPanel()
ap.refresh(["image", "video", "audio"])
check(
    "image" in ap._cat_panels and "video" in ap._cat_panels and "audio" in ap._cat_panels,
    "advanced panel builds per-category sub-panels",
)

print("ALL ADVANCED CHECKS PASSED")
sys.exit(0)
