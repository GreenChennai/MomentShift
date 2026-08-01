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

    # ---------------------------------------------------------------- v0.7.4
    step("v0.7.4 Bug: CollapsibleCard._apply_expanded/_apply_collapsed flip _collapsed")
    from momentshift.gui.theme import CollapsibleCard
    card = CollapsibleCard("t", "", None, collapsed=True)
    assert card.isCollapsed(), "constructed collapsed"
    card._apply_expanded()
    assert card.isCollapsed() is False, "_apply_expanded must set _collapsed=False"
    card._apply_collapsed()
    assert card.isCollapsed() is True, "_apply_collapsed must set _collapsed=True"
    card.deleteLater()

    step("v0.7.4 Bug: setCollapsed flips flag (the adv-switch path)")
    card2 = CollapsibleCard("t2", "", None, collapsed=True)
    card2.setCollapsed(True)   # no-op when equal
    assert card2.isCollapsed()
    card2.setCollapsed(False)
    assert card2.isCollapsed() is False
    card2.deleteLater()

    step("v0.7.4 Adj1: ext_badge renders the suffix text in a brand-tinted rect")
    from momentshift.gui.theme import ext_badge
    b = ext_badge("png")
    assert b.text() == "PNG", b.text()
    assert "rgba(35,134,54,0.08)" in b.styleSheet()
    b.deleteLater()

    step("v0.7.4 Adj1: queue/compress rows use suffix badge (not a pixmap icon)")
    from momentshift.gui.queue_widget import QueueItemWidget
    tw = QueueItemWidget(
        Task(id="t2", input_path=png, output_path=os.path.join(out, "photo.jpg"),
             target_format="jpg", category="image", use_gpu=False)
    )
    assert tw.iconLbl.text() == "PNG", f"expected badge text PNG, got {tw.iconLbl.text()!r}"
    tw.deleteLater()
    row2 = compress.listWidget.items[cimg]
    assert row2.iconLbl.text() == "PNG", f"compress badge text, got {row2.iconLbl.text()!r}"

    step("v0.7.4 Adj2: each interface wires ScrollAutoFollow to its queue scroll")
    from momentshift.gui.queue_widget import ScrollAutoFollow
    for iface in (convert, compress, upscale):
        af = getattr(iface, "_queue_auto_follow", None)
        assert isinstance(af, ScrollAutoFollow), type(iface).__name__
        assert af._scroll is iface.queueScroll, type(iface).__name__
    # ensure() is a safe no-op when not active (no crash, no scroll)
    convert._queue_auto_follow.ensure(None)

    # ---------------------------------------------------------------- v0.7.5
    step("v0.7.5: 引擎注册表加载（14 个引擎：超分 + 插帧）")
    from momentshift.core import engines as eng_mod
    from momentshift.i18n.translator import tr
    assert len(eng_mod.ENGINES) == 14, len(eng_mod.ENGINES)
    assert len(eng_mod.ENGINE_BY_ID) == 14, len(eng_mod.ENGINE_BY_ID)
    sr = [e for e in eng_mod.ENGINES if e.category == "sr"]
    it = [e for e in eng_mod.ENGINES if e.category == "interp"]
    assert sr and it, "必须同时有超分与插帧引擎"
    assert "realesrgan-ncnn-vulkan" in eng_mod.ENGINE_BY_ID
    assert "rife-ncnn-vulkan" in eng_mod.ENGINE_BY_ID

    step("v0.7.5: EnginesCard 可离屏安全构造并重新检测")
    from momentshift.gui.engine_card import EnginesCard
    ec = EnginesCard(None, on_changed=lambda: None)
    ec.rescan()
    ec.deleteLater()

    step("v0.7.5: 动态参数面板按 schema 生成控件，无引擎返回空")
    from momentshift.gui.upscale_interface import EngineParamPanel
    panel = EngineParamPanel(None)
    eng = eng_mod.ENGINE_BY_ID["realesrgan-ncnn-vulkan"]
    panel.build(eng, eng_mod.default_values(eng.eid))
    assert len(panel._controls) == len(eng.params), (len(panel._controls), len(eng.params))
    vals = panel.values()
    assert set(vals.keys()) == {p.key for p in eng.params}, vals
    panel.build(None)
    assert panel.values() == {}, "无引擎时必须返回空参数"
    panel.deleteLater()

    step("v0.7.5: 无引擎回退（放大界面隐藏参数、禁用下拉）")
    installed = eng_mod.installed_engines()
    if not installed:
        assert not upscale.modelCombo.isEnabled(), "无引擎时应禁用下拉"
        assert upscale.modelCombo.itemText(0) == tr("upscale.engine.none")
    else:
        assert upscale.modelCombo.isEnabled()

    step("v0.7.5: RTX 驱动级引擎的 CLI 守卫（无命令行接口）")
    cmd, err = eng_mod.build_command("rtx-super-resolution", "a.png", "b.png", {})
    assert not cmd and err, (cmd, err)

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
