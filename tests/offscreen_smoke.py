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
  - Convert (v0.2.7 redesign): files are expanded/filtered by category, the
    format picker (FormatGrid) is seeded from the default selection, and the
    setup dialog's confirm pushes tasks into the queue via ConversionManager.
    The full ConvertSetupDialog (which builds an AdvancedPanel with CJK combo
    items) hard-kills this sandbox, so it is exercised on a real desktop / CI;
    here we test the safe pieces it delegates to (no repaint, no native combos).
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

    step("Convert: expand_paths() filters out unsupported files")
    bad = os.path.join(tmp, "secret_file.xyz")
    open(bad, "wb").write(b"nope")
    from momentshift.core.presets import IMAGE_EXTS
    assert convert._expand_paths([bad], IMAGE_EXTS) == [], "unsupported file must be filtered"

    step("Convert: FormatGrid seeded from default selection")
    from momentshift.gui.format_grid import FormatGrid
    png = os.path.join(src, "photo.png")
    open(png, "wb").write(b"\x89PNG\r\n\x1a\n")
    fg = FormatGrid(convert)
    fg.setup(["image"], convert._selection)
    assert fg.get_selection().get("image") == "jpg", fg.get_selection()
    fg.deleteLater()

    step("QueueItemWidget constructs (the old crash site, no paint)")
    from momentshift.gui.queue_widget import QueueItemWidget
    from momentshift.core.models import Task
    tw = QueueItemWidget(
        Task(id="t1", input_path=png, output_path=os.path.join(out, "photo.jpg"),
             target_format="jpg", category="image", use_gpu=False)
    )
    tw.deleteLater()

    step("Convert: confirm pushes task into conversion queue (same-folder mode)")
    cfg.outputMode.value = "same"
    cfg.outputSuffix.value = ""
    cfg.outputFolder.value = out
    before = len(manager.tasks)
    added, skipped = manager.add_files([png], "jpg", None, False, "same", "")
    assert len(added) == 1 and skipped == [], (added, skipped)
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

    step("Upscale: files go straight to queue (no staging)")
    img = os.path.join(src, "big.png")
    open(img, "wb").write(b"\x89PNG\r\n\x1a\n")
    upscale._on_files([img])
    assert len(upscale._items) == 1, upscale._items

    step("Compress: staging accepts images")
    cimg = os.path.join(src, "c.png")
    open(cimg, "wb").write(b"\x89PNG\r\n\x1a\n")
    compress._on_files([cimg])
    assert len(compress._items) == 1, compress._items

    # ---------------------------------------------------------------- v0.7.3
    step("v0.7.3 Bug1: pickers resolve a real dialog parent (never None)")
    for iface in (convert, compress, upscale):
        assert iface._dialog_parent() is not None, type(iface).__name__

    step("v0.7.3 Bug1: DropArea defers the drop to the next event loop turn")
    import inspect
    from momentshift.gui.drop_area import DropArea
    drop_src = inspect.getsource(DropArea.dropEvent)
    assert "singleShot" in drop_src, "dropEvent must not emit synchronously"

    step("v0.7.3 Bug2: CollapsibleCard(collapsed=True) collapses without anim")
    from momentshift.gui.theme import CollapsibleCard
    card = CollapsibleCard("t", "", None, collapsed=True)
    assert card._anim is None, "no animation may run at construction time"
    assert card._body.maximumHeight() == 0, card._body.maximumHeight()
    assert card.isCollapsed()
    card.deleteLater()

    step("v0.7.3 Bug3: backend sections carry headers, released height cap")
    for grp in (compress.oxipngGroup, compress.joGroup, compress.pilGroup):
        assert hasattr(grp, "_header"), "backend group needs a section header"
    compress._on_program("auto")
    assert compress.oxipngGroup.isVisibleTo(compress._backend_container)
    assert compress.oxipngGroup._header.isVisibleTo(compress.oxipngGroup)
    compress._on_program("pillow")
    assert not compress.pilGroup._header.isVisibleTo(compress.pilGroup)
    compress._on_program("auto")

    step("v0.7.3 Bug4: compress row mirrors convert row, full bar when done")
    row = compress.listWidget.items[cimg]
    assert hasattr(row, "fmtPill") and hasattr(row, "iconLbl")
    assert row.fmtPill.text().startswith(".PNG"), row.fmtPill.text()
    row.set_progress(37)
    row.set_status("done", saved=1234)
    assert row.prog._value == 100, row.prog._value

    step("v0.7.3 Adj1: no widget exposes a hover tooltip")
    from PyQt6.QtWidgets import QWidget as _QW
    for iface in (convert, compress, upscale, setting, about):
        tipped = [w for w in iface.findChildren(_QW) if w.toolTip()]
        assert not tipped, f"{type(iface).__name__}: {[type(w).__name__ for w in tipped]}"

    step("v0.7.3 Adj2: FormatPill uses the #3EB68F brand background")
    from momentshift.gui.queue_widget import FormatPill
    assert "#3eb68f" in FormatPill(".A → .B").styleSheet().lower()

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
