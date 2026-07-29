"""Core-level enqueue test (no GUI widgets) — verifies the engine logic.

Confirms ConversionManager.add_files accepts a real file, builds a Task, and
emits the expected signals, independent of offscreen widget rendering.
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication

from momentshift.core.queue import ConversionManager
from momentshift.core.config import cfg


def main():
    app = QCoreApplication(sys.argv)

    manager = ConversionManager()

    added_sig = []
    changed_sig = []
    manager.task_added.connect(lambda t: added_sig.append(t))
    manager.queue_changed.connect(lambda: changed_sig.append(1))

    tmp = tempfile.mkdtemp()
    good = os.path.join(tmp, "photo.png")
    with open(good, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")

    added, skipped = manager.add_files([good], "jpg", None, False)
    assert len(added) == 1, added
    assert skipped == [], skipped
    assert manager.tasks[0].target_format == "jpg"
    assert manager.tasks[0].input_path.endswith("photo.png")
    assert added_sig and changed_sig, "signals should have fired"

    # unsupported file is skipped, not added
    bad = os.path.join(tmp, "x.xyz")
    with open(bad, "wb") as fh:
        fh.write(b"x")
    added2, skipped2 = manager.add_files([bad], "jpg", None, False)
    assert added2 == [], added2
    assert skipped2 == ["x.xyz"], skipped2

    # manager.tasks still has exactly 1
    assert len(manager.tasks) == 1
    manager.clear()
    assert manager.tasks == []

    print("CORE ENQUEUE OK — add_files builds Task, fires signals, "
          "skips unsupported, clear works.")
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        os._exit(1)
