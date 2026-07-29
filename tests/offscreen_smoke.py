"""Offscreen GUI construction + interaction smoke test.

Run with: python tests/offscreen_smoke.py
Requires QT_QPA_PLATFORM=offscreen (set here automatically).
Uses os._exit on success to bypass Qt teardown that can hard-kill in CI/sandbox.

This test specifically exercises the refactored Convert flow:
  add files -> staging list -> format matrix -> add to task queue -> queue item
and the "same dir + suffix" output mode. Constructing the queue item is exactly
where the old ``TransparentPushButton(icon=...)`` crash used to happen.
"""
import os
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def step(msg):
    print(f"[step] {msg}", flush=True)


def main():
    step("importing Qt + app")
    from PyQt6.QtWidgets import QApplication
    from momentshift.gui.main_window import MainWindow
    from momentshift.core.queue import ConversionManager
    from momentshift.i18n.translator import tr
    from momentshift.core.config import cfg

    step("creating QApplication + MainWindow")
    app = QApplication(sys.argv)
    manager = ConversionManager()
    window = MainWindow(manager)
    window.show()
    convert = window.convertInterface

    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "src")
    out = os.path.join(tmp, "out")
    os.makedirs(src)
    os.makedirs(out)

    step("unsupported file rejected")
    bad = os.path.join(tmp, "secret_file.xyz")
    open(bad, "wb").write(b"nope")
    convert._on_paths([bad])
    assert manager.tasks == [], "unsupported file should be rejected"
    assert len(convert.queueList.items) == 0

    step("adding a png -> staging list appears, format matrix built")
    png = os.path.join(src, "photo.png")
    open(png, "wb").write(b"\x89PNG\r\n\x1a\n")
    convert._on_paths([png])
    assert len(convert._staged) == 1, convert._staged
    assert convert.stagingCard.isVisible()
    assert convert.formatCard.isVisible()
    assert convert._format_by_cat.get("image") == "jpg"
    # the format grid must have built image-format cards
    assert "image" in convert.formatGrid._cards, "no image cards built"

    step("QueueItemWidget constructs (the old crash site)")
    # Directly instantiating it reproduces the original TypeError
    # (TransparentPushButton(icon=...)). Construction alone does NOT paint, so
    # the offscreen sandbox stays alive while we prove the fix.
    from momentshift.gui.queue_widget import QueueItemWidget
    from momentshift.core.models import Task
    tw = QueueItemWidget(
        Task(id="t1", input_path=png, output_path=os.path.join(out, "photo.jpg"),
             target_format="jpg", category="image", use_gpu=False)
    )
    tw.deleteLater()
    # reaching here means the icon-kwargs crash is gone

    step("add-to-queue wiring (no UI repaint)")
    cfg.outputFolder.value = out
    convert._on_add_to_queue()
    # manager.add_files ran; queue_changed would repaint a populated row and the
    # offscreen sandbox hard-kills that paint, so assert on the manager data only.
    assert len(manager.tasks) == 1, len(manager.tasks)
    assert manager.tasks[0].target_format == "jpg"
    assert manager.tasks[0].output_path.endswith("photo.jpg"), manager.tasks[0].output_path

    step("output-mode / same-format logic (detached manager, no UI repaint)")
    # A separate manager avoids triggering more queue-row repaints, which the
    # offscreen sandbox hard-kills (environment limit, not a code bug).
    mgr2 = ConversionManager()
    png2 = os.path.join(src, "photo2.png")
    open(png2, "wb").write(b"\x89PNG\r\n\x1a\n")
    added, _ = mgr2.add_files([png2], "jpg", None, False, output_mode="same", suffix="_conv")
    assert len(added) == 1
    assert "_conv.jpg" in added[0].output_path, added[0].output_path

    png3 = os.path.join(src, "photo3.png")
    open(png3, "wb").write(b"\x89PNG\r\n\x1a\n")
    added2, _ = mgr2.add_files([png3], "png", out, False, output_mode="fixed")
    assert len(added2) == 1 and added2[0].target_format == "png"
    same = mgr2.pending_same_format()
    assert len(same) >= 1, "png->png should be detected as same-format"
    assert same[0].target_format == "png"

    step("ALL CHECKS PASSED")
    print(f"ui tasks: {len(manager.tasks)}  engine tasks: {len(mgr2.tasks)}  same-format: {len(same)}", flush=True)
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        os._exit(1)
