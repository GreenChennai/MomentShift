"""Offscreen construction + flow smoke test (no window paint).

Run with: python tests/offscreen_smoke.py
Requires QT_QPA_PLATFORM=offscreen (set here automatically).
Uses os._exit on success to bypass Qt teardown that can hard-kill in CI/sandbox.

NOTE: this sandbox hard-kills (exit 127) if a *paint* of the full FluentWindow /
a populated queue row / an InfoBar is attempted. Constructing widgets without
calling show() does NOT paint, so we validate every interface's __init__ + retheme
chain and the Convert flow by building the interfaces standalone (no FluentWindow).
Full-window visual verification belongs on a real desktop / GitHub Actions.

Covers:
  - All five interfaces import and construct (rebuilt UI).
  - Convert: add files -> staging list -> format matrix built -> add to queue ->
    engine receives the task (no repaint, so safe).
  - Detached manager: output-mode + same-format logic.
  - Upscale staging accepts media.
"""

import os
import sys
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def step(msg):
    print(f"[step] {msg}", flush=True)


def main():
    step("importing Qt")
    from PyQt6.QtWidgets import QApplication
    from momentshift.gui.convert_interface import ConvertInterface
    from momentshift.gui.compress_interface import CompressInterface
    from momentshift.gui.upscale_interface import UpscaleInterface
    from momentshift.gui.setting_interface import SettingInterface
    from momentshift.gui.about_interface import AboutInterface
    from momentshift.core.queue import ConversionManager
    from momentshift.core.config import cfg

    step("creating QApplication")
    app = QApplication(sys.argv)
    manager = ConversionManager()

    step("constructing all five interfaces (standalone, no paint)")
    convert = ConvertInterface(manager)
    compress = CompressInterface()
    upscale = UpscaleInterface()
    setting = SettingInterface()
    about = AboutInterface()
    for iface in (convert, compress, upscale):
        assert iface.dropArea is not None, f"{type(iface).__name__} missing dropArea"
    step("all interfaces constructed OK")

    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "src")
    out = os.path.join(tmp, "out")
    os.makedirs(src)
    os.makedirs(out)

    step("Convert: unsupported file rejected by staging")
    bad = os.path.join(tmp, "secret_file.xyz")
    open(bad, "wb").write(b"nope")
    convert._on_files([bad])
    assert len(convert._staged) == 0, "unsupported file should be rejected"
    assert len(convert.queueList.items) == 0

    step("Convert: png -> staging + format matrix built")
    png = os.path.join(src, "photo.png")
    open(png, "wb").write(b"\x89PNG\r\n\x1a\n")
    convert._on_files([png])
    assert len(convert._staged) == 1, convert._staged
    # format matrix is built from staging (visibility is irrelevant when offscreen)
    assert convert.formatGrid.get_selection().get("image") == "jpg"
    assert any(c.category == "image" for c in convert.formatGrid._cards), "no image cards"

    step("QueueItemWidget constructs (the old crash site, no paint)")
    from momentshift.gui.queue_widget import QueueItemWidget
    from momentshift.core.models import Task
    tw = QueueItemWidget(
        Task(id="t1", input_path=png, output_path=os.path.join(out, "photo.jpg"),
             target_format="jpg", category="image", use_gpu=False)
    )
    tw.deleteLater()

    step("Convert: add-to-queue wiring (engine receives task, no repaint)")
    cfg.outputFolder.value = out
    before = len(manager.tasks)
    convert._on_add_to_queue()
    assert len(manager.tasks) == before + 1, len(manager.tasks)
    assert manager.tasks[-1].target_format == "jpg"
    assert manager.tasks[-1].output_path.endswith("photo.jpg"), manager.tasks[-1].output_path

    step("detached manager: output-mode + same-format logic")
    mgr2 = ConversionManager()
    png2 = os.path.join(src, "photo2.png")
    open(png2, "wb").write(b"\x89PNG\r\n\x1a\n")
    added, _ = mgr2.add_files([png2], "jpg", None, False, output_mode="same", suffix="_conv")
    assert len(added) == 1 and "_conv.jpg" in added[0].output_path, added[0].output_path

    png3 = os.path.join(src, "photo3.png")
    open(png3, "wb").write(b"\x89PNG\r\n\x1a\n")
    added2, _ = mgr2.add_files([png3], "png", out, False, output_mode="fixed")
    assert len(added2) == 1 and added2[0].target_format == "png"
    same = mgr2.pending_same_format()
    assert len(same) >= 1 and same[0].target_format == "png"

    step("Upscale: staging accepts media")
    img = os.path.join(src, "big.png")
    open(img, "wb").write(b"\x89PNG\r\n\x1a\n")
    upscale._on_files([img])
    assert len(upscale._staged) == 1, upscale._staged

    step("Compress: staging accepts images")
    cimg = os.path.join(src, "c.png")
    open(cimg, "wb").write(b"\x89PNG\r\n\x1a\n")
    compress._on_files([cimg])
    assert len(compress._items) == 1, compress._items

    step("ALL CHECKS PASSED")
    print(f"convert engine tasks: {len(manager.tasks)}  detached tasks: {len(mgr2.tasks)}  "
          f"same-format: {len(same)}", flush=True)
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        os._exit(1)
