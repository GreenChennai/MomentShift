"""v0.8.0 批次 A2 定点验证（离屏）。

只覆盖 A2 触碰到的路径，不重复 offscreen_smoke 已经守住的东西：

- RISK-01  放大队列行「复制路径」不再 NameError
- RISK-02  对比控件封面改后台线程解码
- RISK-03  advanced 全局字典加锁 + 入队深拷贝快照
- INFRA-04 设置页「打开配置文件」走系统默认程序
- ODD-01   恒真三元 / 魔法色清理
- ODD-02   链式元组赋值清理
- ODD-03   ComboSettingCard 外部变更用 findData 定位
- ODD-04   快速调用轮询转发改异步
- ODD-07   快速调用改走公开 API

沙箱注意：绝不构造 ConvertSetupDialog（会 exit 127），不填队列行、不弹 InfoBar。
"""

from __future__ import annotations

import inspect
import io
import os
import sys
import textwrap
import tokenize
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"[ok] {label}")
    else:
        FAILED.append(label)
        print(f"[FAIL] {label}")


# ---------------------------------------------------------------------------
# 源码断言工具
# ---------------------------------------------------------------------------
def code_of(obj: object) -> str:
    """取对象源码中「真正会被执行的代码」，剥掉行注释与文档字符串。

    A2 的修复注释按团队要求写的是「为什么」，因此大量原样引用了被替换掉的旧
    写法作为佐证。如果断言直接对 inspect.getsource() 做子串匹配，注释里的旧
    片段会被误判成「没改干净」——这正是本文件第一版 7 个假阳性的成因。这里做
    词法级剥离：注释和 docstring 的字符全部替换成空格（换行保留），其余字节
    与原文逐字对齐，因此列号、缩进、子串边界都不受影响。
    """
    src = obj if isinstance(obj, str) else inspect.getsource(obj)
    src = textwrap.dedent(src)
    lines = src.splitlines(keepends=True)
    tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))

    skippable = {tokenize.NL, tokenize.COMMENT}
    stmt_head = {
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
    }
    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    prev_type = tokenize.ENCODING

    for i, tok in enumerate(tokens):
        drop = tok.type == tokenize.COMMENT
        if not drop and tok.type == tokenize.STRING and prev_type in stmt_head:
            # 语句位置上的裸字符串 = docstring（或无副作用的注释式字符串）
            nxt = next((t for t in tokens[i + 1 :] if t.type not in skippable), None)
            drop = nxt is not None and nxt.type in (
                tokenize.NEWLINE,
                tokenize.ENDMARKER,
            )
        if drop:
            spans.append((tok.start, tok.end))
        prev_type = tok.type

    for (start_row, start_col), (end_row, end_col) in spans:
        for row in range(start_row, end_row + 1):
            line = lines[row - 1]
            lo = start_col if row == start_row else 0
            hi = end_col if row == end_row else len(line)
            blanked = "".join(" " if ch != "\n" else "\n" for ch in line[lo:hi])
            lines[row - 1] = line[:lo] + blanked + line[hi:]
    return "".join(lines)


def test_code_of_helper() -> None:
    """先自测剥离器本身，否则后面所有源码断言都不可信。"""
    sample = (
        "def f():\n"
        '    """doc 里提到 not False 和 subprocess.Popen("""\n'
        "    x = 1  # 注释里也写 not False\n"
        '    return tr("settings.reset_confirm")\n'
    )
    stripped = code_of(sample)
    check("not False" not in stripped, "self: docstring/注释里的旧片段被剥离")
    check("subprocess.Popen(" not in stripped, "self: docstring 内调用样例被剥离")
    check('tr("settings.reset_confirm")' in stripped, "self: 真实代码里的字符串字面量保留")
    check("x = 1" in stripped, "self: 代码行原样保留")


# ---------------------------------------------------------------------------
# RISK-03 / Q3：advanced 快照
# ---------------------------------------------------------------------------
def test_advanced_snapshot() -> None:
    from momentshift.core import advanced

    advanced.reset()
    snap = advanced.snapshot("image")
    advanced.adv["image"]["compress"]["quality"] = 11
    check(snap["compress"]["quality"] == 95, "RISK-03/Q3: snapshot 深拷贝，改全局不串台")
    check(
        advanced.get("image") is advanced.adv["image"],
        "RISK-03: get() 仍返回可写实时引用（面板需要）",
    )

    identity = advanced.adv["image"]["compress"]
    advanced.reset()
    check(
        advanced.adv["image"]["compress"] is identity,
        "RISK-03: reset() 原地重建，子字典对象标识不变",
    )
    check(advanced.adv["image"]["compress"]["quality"] == 95, "RISK-03: reset() 恢复嵌套默认值")


def test_enqueue_snapshot_isolation() -> None:
    """入队后再改面板，已入队任务的 adv 不得跟着变（Q3 可观察行为变更）。"""
    import tempfile

    from momentshift.core import advanced
    from momentshift.core.queue import ConversionManager

    advanced.reset()
    advanced.adv["image"]["compress"]["quality"] = 42

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "a.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        mgr = ConversionManager()
        added, _ = mgr.add_files([str(src)], "jpg", td, False, output_mode="fixed")
        check(len(added) == 1, "Q3: 入队成功")
        task = added[0]
        check(task.adv["compress"]["quality"] == 42, "Q3: 入队时定格当前参数")

        advanced.adv["image"]["compress"]["quality"] = 7
        check(
            task.adv["compress"]["quality"] == 42, "Q3: 入队后改面板不影响已排队任务（深拷贝生效）"
        )
    advanced.reset()


# ---------------------------------------------------------------------------
# RISK-01 / RISK-02 / ODD-01 / ODD-02 / ODD-03
# ---------------------------------------------------------------------------
def test_gui_fixes(app) -> None:
    from PyQt6.QtGui import QImage

    from momentshift.gui import advanced_panel, compare_widget, convert_setup_dialog
    from momentshift.gui import upscale_interface as ui_mod

    # RISK-01：QApplication 在模块命名空间里可解析
    check(
        getattr(ui_mod, "QApplication", None) is not None,
        "RISK-01: upscale_interface 已导入 QApplication",
    )
    src = code_of(ui_mod.UpscaleItemWidget._copy_path)
    check("QApplication.clipboard()" in src, "RISK-01: _copy_path 仍走剪贴板")

    # RISK-02：封面解码返回 QImage（可跨线程），且 _PosterTask 是 QRunnable
    img = compare_widget._load_poster_image(None)
    check(isinstance(img, QImage) and img.isNull(), "RISK-02: 空路径返回空 QImage 而非 QPixmap")
    from momentshift.core.qt_compat import QRunnable

    check(
        issubclass(compare_widget._PosterTask, QRunnable),
        "RISK-02: 抽帧封装为 QRunnable（离开 GUI 线程）",
    )
    widget = compare_widget.CompareWidget()
    check(
        widget._poster_signals.parent() is widget,
        "RISK-02: WorkerSignals 挂 parent（v0.7.19/24 GC 崩溃教训）",
    )
    widget.set_paths(None, None)  # 不应抛异常，也不应阻塞
    check(widget.label.isVisible() is False, "RISK-02: 无路径时隐藏对比区")
    widget.deleteLater()

    # ODD-01：恒真三元与魔法色已清理
    hdr_src = code_of(advanced_panel._Header)
    check("not False" not in hdr_src, "ODD-01: _Header 无 `not False` 恒真三元")
    check("#1a1a1a" not in hdr_src and "#e8e8e8" not in hdr_src, "ODD-01: _Header 无硬编码文字色")
    check("text_strong()" in hdr_src, "ODD-01: _Header 文字色取自主题令牌")
    from momentshift.gui import format_grid

    grid_src = code_of(format_grid.FormatCard._colors)
    check("not False" not in grid_src, "ODD-01: FormatCard 无 `not False` 恒真三元")
    check(
        "QColor(200, 200, 200)" not in grid_src and "QColor(200,200,200)" not in grid_src,
        "ODD-01: FormatCard 无硬编码描边色",
    )

    # ODD-02：链式元组赋值已拆开
    dlg_src = code_of(convert_setup_dialog.ConvertSetupDialog._build_right)
    check("self._fmt_card" not in dlg_src, "ODD-02: 链式元组赋值已移除")
    check("fmt_vb" not in dlg_src, "ODD-02: 未使用的 fmt_vb 已移除")

    # ODD-03：外部变更用 findData
    from momentshift.gui.setting_interface import ComboSettingCard

    ext_src = code_of(ComboSettingCard._on_external)
    check("findData" in ext_src, "ODD-03: _on_external 改用 findData")
    check(
        "for i in range(self.combo.count())" not in ext_src,
        "ODD-03: 不再用 setCurrentIndex 循环当查找",
    )


# ---------------------------------------------------------------------------
# INFRA-04 / Q4 / Q5
# ---------------------------------------------------------------------------
def test_settings_fixes() -> None:
    from qfluentwidgets import ConfigItem

    from momentshift.core.config import cfg
    from momentshift.gui import setting_interface as si

    open_src = code_of(si.SettingInterface._open_config)
    check("QDesktopServices.openUrl" in open_src, "Q5: 打开配置改走系统默认程序")
    check("popen_silent" in open_src, "INFRA-04: notepad 回退走静默 Popen")
    check("subprocess.Popen(" not in open_src, "INFRA-04: 不再直接裸调 subprocess.Popen")
    check("config_file()" in open_src, "INFRA-04: 路径统一取 core.platform.config_file()")

    reset_src = code_of(si.SettingInterface._reset)
    check("settings.reset_confirm" in reset_src, "Q4: 确认弹窗使用专用文案键")
    check("defaultValue" in reset_src, "Q4: 遍历 ConfigItem 回写默认值")
    check("quick_launch.apply_all" in reset_src, "Q4: 重置同时注销右键菜单")

    items = [n for n, v in vars(type(cfg)).items() if isinstance(v, ConfigItem)]
    check(len(items) >= 20, f"Q4: Config 声明了 {len(items)} 项（≥20）")

    # 文案键在三种语言里齐备
    import json

    for loc in ("zh_CN", "zh_TW", "en_US"):
        p = Path(__file__).resolve().parent.parent / "src/momentshift/i18n/locales" / f"{loc}.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        text = data.get("settings.reset_confirm", "")
        check(bool(text), f"Q4: {loc} 补齐 settings.reset_confirm")


def test_reset_loop_semantics() -> None:
    """不构造设置页，单独验证「遍历 ConfigItem 回写默认值」能覆盖全部项。"""
    from qfluentwidgets import ConfigItem

    from momentshift.core.config import cfg

    touched = []
    originals = {}
    for name, item in vars(type(cfg)).items():
        if isinstance(item, ConfigItem):
            originals[name] = item.value
            touched.append(name)

    check(
        "outputSuffix" in touched and "quickNotifyDone" in touched,
        "Q4: 遍历覆盖到输出后缀与通知开关（旧实现漏掉的项）",
    )

    try:
        cfg.outputSuffix.value = "_zzz"
        cfg.quickNotifyDone.value = False
        for _, item in vars(type(cfg)).items():
            if isinstance(item, ConfigItem):
                item.value = item.defaultValue
        check(cfg.outputSuffix.value == "_converted", "Q4: 输出后缀被重置")
        check(cfg.quickNotifyDone.value is True, "Q4: 通知开关被重置")
    finally:
        for name, value in originals.items():
            getattr(cfg, name).value = value


# ---------------------------------------------------------------------------
# ODD-04 / ODD-07
# ---------------------------------------------------------------------------
def test_quick_runner_fixes() -> None:
    from momentshift import quick_runner as qr

    run_src = code_of(qr.run_quick)
    check("time.sleep" not in run_src, "ODD-04: run_quick 不再同步 sleep 轮询")
    check("_forward_with_retry" in run_src, "ODD-04: 改走异步轮询helper")
    retry_src = code_of(qr._forward_with_retry)
    check(
        "QTimer" in retry_src and "app.exec()" in retry_src,
        "ODD-04: 轮询由 QTimer 驱动且事件循环运转",
    )
    check("time.sleep" not in retry_src, "ODD-04: helper 内也无阻塞 sleep")

    for fn in (qr._run_compress, qr._run_upscale):
        src = code_of(fn)
        check(
            "apply_settings(" in src and "enqueue_and_start(" in src,
            f"ODD-07: {fn.__name__} 改走公开 API",
        )
        check(
            "._program =" not in src and "._engine_id =" not in src,
            f"ODD-07: {fn.__name__} 不再直改私有属性",
        )
        check(
            "._add_item(" not in src and "._add_to_queue(" not in src,
            f"ODD-07: {fn.__name__} 不再直调私有方法",
        )


def test_public_api_roundtrip(app) -> None:
    """两个界面实例之间 export → apply 的往返一致性（ODD-07 的实际契约）。"""
    from momentshift.gui.compress_interface import CompressInterface
    from momentshift.gui.upscale_interface import UpscaleInterface

    a = CompressInterface(None)
    b = CompressInterface(None)
    a._program = "pillow"
    a._target = "webp"
    a._output_mode = "fixed"
    a._suffix = "_x"
    a._folder = "D:/out"
    a._tool_opts["pillow"]["pil_quality"] = 71

    settings = a.export_settings()
    b.apply_settings(settings)
    check(b._program == "pillow" and b._target == "webp", "ODD-07: 压缩 export/apply 往返一致")
    check(b._tool_opts["pillow"]["pil_quality"] == 71, "ODD-07: 压缩嵌套参数一并带过去")
    a._tool_opts["pillow"]["pil_quality"] = 9
    check(
        b._tool_opts["pillow"]["pil_quality"] == 71, "ODD-07: 压缩参数深拷贝，两个实例不共享子字典"
    )
    check(hasattr(b, "enqueue_and_start"), "ODD-07: 压缩暴露 enqueue_and_start")

    u1 = UpscaleInterface(None)
    u2 = UpscaleInterface(None)
    u1._fmt = "webp"
    u1._suffix = "_up"
    u1._folder = "D:/up"
    u2.apply_settings(u1.export_settings())
    check(
        u2._fmt == "webp" and u2._suffix == "_up" and u2._folder == "D:/up",
        "ODD-07: 放大 export/apply 往返一致",
    )
    check(hasattr(u2, "enqueue_and_start"), "ODD-07: 放大暴露 enqueue_and_start")

    for w in (a, b, u1, u2):
        w.deleteLater()


# ---------------------------------------------------------------------------
def main() -> int:
    from momentshift.app_bootstrap import create_application

    test_code_of_helper()
    test_advanced_snapshot()
    test_enqueue_snapshot_isolation()
    test_settings_fixes()
    test_reset_loop_semantics()
    test_quick_runner_fixes()

    app = create_application(sys.argv)
    test_gui_fixes(app)
    test_public_api_roundtrip(app)

    if FAILED:
        print(f"\n{len(FAILED)} CHECK(S) FAILED:")
        for f in FAILED:
            print("  -", f)
        return 1
    print("\nA2 ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
