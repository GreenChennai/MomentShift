"""QSS 视觉快照工具 —— B1 令牌化改造的「视觉零回归」证据。

职责边界：
- 做：离屏构造全部界面与自绘控件，把每个控件的 ``styleSheet()`` 字符串、
  主题色访问器返回值、以及各 QSS 构建函数的输出，dump 成一份可比对的快照；
  支持「生成基线」与「与基线比对」两种模式。
- 不做：不做像素级渲染比对（沙箱渲染真实 UI 会 exit 127 硬杀）；
  不校验样式是否「好看」，只校验改造前后**样式声明完全一致**。

依赖：momentshift.gui 全部界面模块；被依赖：无（独立脚本 + pytest 用例）。

为什么需要它：B1 要把 99 处硬编码色替换成语义令牌，方案要求「逐页截图零差异」，
但沙箱渲染不了真实 UI。色值不变则所有 QSS 字符串应逐字节相同 —— 这是等价且更严格的证据。

比对分四档（见 :func:`normalize_qss` 与 :func:`order_equivalent`）：

===========  ==========================================  ============
档位         含义                                        是否判失败
===========  ==========================================  ============
完全一致     原文逐字节相同                              否
仅格式差异   归一化后相同（只差空白），QSS 词法上等价    否（但会逐条列出待人工确认）
仅顺序差异   同一规则块内声明集合相同、仅书写次序不同    否（但会逐条列出待人工确认）
实质差异     选择器、属性集合或色值真的变了              **是**
===========  ==========================================  ============

刻意**不**做十六进制大小写归一：``#FFFFFF`` 与 ``#ffffff`` 视觉虽同，
但 B1 铁律是「只换写法不换颜色」，大小写变动必须暴露出来由人确认。

环境无关性（v0.8.0-B0）：采集与校验都先经 :func:`establish_production_theme`
显式建立与生产同源的主题态并自检，**不依赖** ``config.json`` 是否存在、存了什么。
在此之前基线被绑死在采集机器的磁盘状态上，换环境即全红 —— 详见该函数的 Notes。

「仅顺序差异」这一档是收敛共享构建器时才需要的：构建器按固定次序输出
``color → font-size → font-weight → background``，而改造前各处手写顺序五花八门。
判定时会先确认块内**没有重复属性名**，确保不存在「后一条覆盖前一条」的层叠语义，
否则一律降级为实质差异。

典型用法::

    # 改造前生成基线
    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python tests/qss_snapshot.py --write
    # 改造后比对
    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python tests/qss_snapshot.py --check
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

BASELINE = Path(__file__).parent / "qss_baseline.json"

_WS_RUN = re.compile(r"\s+")
_WS_AROUND_PUNCT = re.compile(r"\s*([:;{},])\s*")


def normalize_qss(text: str) -> str:
    """把 QSS 归一化成「词法等价」形式，用于区分格式差异与实质差异。

    Args:
        text: 原始 QSS 字符串。

    Returns:
        压掉冗余空白后的等价字符串。

    Notes:
        只做两件事：连续空白压成一个空格；``: ; { } ,`` 两侧的空白删掉。
        这两步在 QSS 词法层面都是无损的（``padding: 1px 4px`` 里值与值之间的
        单个空格会被保留，不会被误粘成 ``1px4px``）。
        **不**做大小写归一，也**不**排序声明顺序 —— 前者要暴露色值写法变动，
        后者能暴露"后一条声明覆盖前一条"这类真实的层叠改变。
    """
    out = _WS_RUN.sub(" ", text)
    out = _WS_AROUND_PUNCT.sub(r"\1", out)
    return out.strip()


def _split_declarations(chunk: str) -> list[tuple[str, str]] | None:
    """把 ``a:1;b:2;`` 拆成 ``[("a", "1"), ("b", "2")]``。

    Args:
        chunk: 已归一化的声明串（不含花括号）。

    Returns:
        属性值对列表；出现无法按 ``属性:值`` 解析的片段时返回 ``None``。
    """
    decls: list[tuple[str, str]] = []
    for piece in chunk.split(";"):
        if not piece.strip():
            continue
        if ":" not in piece:
            return None
        prop, _, value = piece.partition(":")
        decls.append((prop.strip(), value.strip()))
    return decls


def parse_blocks(text: str) -> list[tuple[str, list[tuple[str, str]]]] | None:
    """把 QSS 拆成 ``[(选择器, 声明列表), ...]``。

    Args:
        text: 原始 QSS 字符串。

    Returns:
        规则块列表；花括号不配对等无法可靠解析的情况返回 ``None``
        （调用方据此放弃顺序等价判定，直接按实质差异处理）。

    Notes:
        无花括号的裸声明串（QLabel 上最常见的写法）视为一个选择器为空的块。
    """
    norm = normalize_qss(text)
    if "{" not in norm:
        decls = _split_declarations(norm)
        return None if decls is None else [("", decls)]

    blocks: list[tuple[str, list[tuple[str, str]]]] = []
    cursor = 0
    while cursor < len(norm):
        open_at = norm.find("{", cursor)
        if open_at < 0:
            # 尾部若还有非空白残留，说明结构超出简单文法，放弃解析
            return None if norm[cursor:].strip() else blocks
        close_at = norm.find("}", open_at)
        if close_at < 0:
            return None
        selector = norm[cursor:open_at].strip()
        decls = _split_declarations(norm[open_at + 1 : close_at])
        if decls is None:
            return None
        blocks.append((selector, decls))
        cursor = close_at + 1
    return blocks


def order_equivalent(left: str, right: str) -> bool:
    """判断两段 QSS 是否「仅声明顺序不同」而层叠结果完全一致。

    Args:
        left: 基线 QSS。
        right: 当前 QSS。

    Returns:
        选择器序列一致、且每个块内声明集合完全相同时为 ``True``。

    Notes:
        只要任一块内出现**重复属性名**就直接返回 ``False``——那种情况下
        后写的声明会覆盖先写的，次序本身带有语义，不允许被当作等价。
    """
    lb, rb = parse_blocks(left), parse_blocks(right)
    if lb is None or rb is None or len(lb) != len(rb):
        return False
    for (l_sel, l_decls), (r_sel, r_decls) in zip(lb, rb, strict=True):
        if l_sel != r_sel:
            return False
        l_props = [p for p, _ in l_decls]
        r_props = [p for p, _ in r_decls]
        if len(set(l_props)) != len(l_props) or len(set(r_props)) != len(r_props):
            return False
        if dict(l_decls) != dict(r_decls):
            return False
    return True


# --------------------------------------------------------------------------
# 控件树遍历
# --------------------------------------------------------------------------
def _widget_key(widget: Any, index: int) -> str:
    """生成稳定的控件标识：``序号|类名#objectName``。

    Args:
        widget: 目标控件。
        index: 在遍历序列中的序号，用于消除同类同名控件的歧义。

    Returns:
        可跨运行复现的键名。
    """
    name = widget.objectName() or "-"
    return f"{index:04d}|{type(widget).__name__}#{name}"


def dump_tree(root: Any, prefix: str) -> dict[str, str]:
    """深度遍历控件树，收集所有非空 ``styleSheet()``。

    Args:
        root: 根控件。
        prefix: 快照键前缀（通常是界面名）。

    Returns:
        ``{键: QSS 字符串}``；只记录非空样式，避免快照被空串淹没。

    Notes:
        使用 ``findChildren`` 而非递归，Qt 保证其返回顺序与构造顺序一致，
        因此快照在改造前后可稳定对齐。
    """
    from PyQt6.QtWidgets import QWidget

    out: dict[str, str] = {}
    qss = root.styleSheet()
    if qss:
        out[f"{prefix}/{_widget_key(root, 0)}"] = qss
    for i, child in enumerate(root.findChildren(QWidget), start=1):
        qss = child.styleSheet()
        if qss:
            out[f"{prefix}/{_widget_key(child, i)}"] = qss
    return out


# --------------------------------------------------------------------------
# 各采集分区
# --------------------------------------------------------------------------
def collect_tokens() -> dict[str, str]:
    """采集 ``gui/tokens.py`` 里全部公开视觉令牌的取值。

    Returns:
        ``{"tokens.名字": 值}``。

    Notes:
        为什么单独采一份令牌表：其余分区采的是「控件渲染出来的 QSS」，只能覆盖到
        **被实际构造的控件用到的**令牌。像 ``ACCENT_SOFT_FAINT``（斑马纹底）、
        ``ACCENT_SOFT_STRONG``（选中态底）这类只在运行时特定状态下才出现的值，
        以及沙箱禁止构造的 ``ConvertSetupDialog`` 用到的 ``ACCENT_TINT_*``，
        全都落在快照视野之外 —— 改错了不会有任何告警。
        直接把令牌表本身纳入快照，等于给"全局颜色映射表"上锁，成本极低。
    """
    from momentshift.gui import tokens

    out: dict[str, str] = {}
    for name in sorted(dir(tokens)):
        if not name.isupper():
            continue
        val = getattr(tokens, name)
        if isinstance(val, (str, int, float)):
            out[f"tokens.{name}"] = str(val)
    return out


def collect_theme_api() -> dict[str, str]:
    """采集 theme.py 全部颜色访问器与常量的当前取值。"""
    from momentshift.gui import theme

    out: dict[str, str] = {}
    accessors = [
        "content_bg",
        "component_bg",
        "surface",
        "surface_hover",
        "surface_pressed",
        "accent_color",
        "accent_name",
        "text_strong",
        "text_secondary",
        "placeholder_text",
        "text_disabled",
        "muted_text",
        "sub_text",
        "hint_text",
        "link_color",
        "border_color",
        "border_hover",
        "danger_color",
        "danger_text",
        "success_color",
        "success_text",
    ]
    for fn_name in accessors:
        fn = getattr(theme, fn_name, None)
        if fn is None:
            out[f"theme.{fn_name}()"] = "<缺失>"
            continue
        val = fn()
        out[f"theme.{fn_name}()"] = val.name() if hasattr(val, "name") else str(val)

    consts = [
        "WINDOW_BG",
        "SURFACE",
        "SURFACE_HOVER",
        "SURFACE_PRESS",
        "TEXT_STRONG",
        "TEXT_SECONDARY",
        "TEXT_PLACEHOLDER",
        "TEXT_MUTED",
        "TEXT_LINK",
        "BORDER_COLOR",
        "BORDER_HOVER",
        "ACCENT",
        "ACCENT_HEX",
        "COLOR_DANGER",
        "COLOR_SUCCESS",
        "DANGER_TEXT",
        "SUCCESS_TEXT",
        "RADIUS",
        "SPACING",
        "CARD_MARGIN",
    ]
    for const in consts:
        val = getattr(theme, const, None)
        if val is None:
            out[f"theme.{const}"] = "<缺失>"
        else:
            out[f"theme.{const}"] = val.name() if hasattr(val, "name") else str(val)

    out["theme.scrollbar_qss()"] = theme.scrollbar_qss()
    return out


def collect_builders() -> dict[str, str]:
    """采集 theme.py 各构建器产出的控件样式。"""
    from momentshift.gui import theme

    out: dict[str, str] = {}
    out.update(dump_tree(theme.ThemedCard(), "builder/ThemedCard"))
    out.update(dump_tree(theme.CollapsibleCard("标题", "副标题"), "builder/CollapsibleCard"))

    card, _ = theme.panel("面板标题", "面板副标题")
    out.update(dump_tree(card, "builder/panel"))

    from qfluentwidgets import BodyLabel

    out.update(dump_tree(theme.field_row("字段", BodyLabel("值")), "builder/field_row"))
    for ext in ("mp4", "png", "", "gif"):
        badge = theme.ext_badge(ext)
        out[f"builder/ext_badge({ext!r})"] = badge.styleSheet()
    out["builder/section_label"] = theme.section_label("小节").styleSheet()
    return out


def collect_queue_widgets() -> dict[str, str]:
    """采集队列相关自绘控件在全部状态下的样式与配色表。"""
    from momentshift.gui import queue_widget as qw

    out: dict[str, str] = {}
    out["queue/_STATUS_PILL_BG"] = json.dumps(
        qw._STATUS_PILL_BG, ensure_ascii=False, sort_keys=True
    )
    out["queue/_STATUS_PILL_FG"] = qw._STATUS_PILL_FG

    for status in sorted(qw._STATUS_PILL_BG):
        pill = qw.StatusPill(status)
        out[f"queue/StatusPill[{status}]"] = pill.styleSheet()

    # 尺寸对比富文本里内嵌了颜色，属于视觉输出的一部分
    for before, after in ((1000, 500), (500, 1000), (1000, 1000), (0, 500), (500, 0)):
        out[f"queue/format_size_compare({before},{after})"] = qw.format_size_compare(before, after)

    bar = qw.ProgressBar()
    out["queue/ProgressBar"] = bar.styleSheet()
    out.update(dump_tree(qw.QueueListWidget(), "queue/QueueListWidget"))
    return out


def collect_interfaces() -> dict[str, str]:
    """采集五大界面构造完成后的全量控件样式。

    Notes:
        只构造不 ``show()``，因此不触发绘制，符合沙箱限制；
        绝不构造 ``ConvertSetupDialog``（离屏会 exit 127 硬杀）。
    """
    from momentshift.core.queue import ConversionManager
    from momentshift.gui.about_interface import AboutInterface
    from momentshift.gui.compress_interface import CompressInterface
    from momentshift.gui.convert_interface import ConvertInterface
    from momentshift.gui.setting_interface import SettingInterface
    from momentshift.gui.upscale_interface import UpscaleInterface

    out: dict[str, str] = {}
    manager = ConversionManager()
    out.update(dump_tree(ConvertInterface(manager), "iface/Convert"))
    out.update(dump_tree(CompressInterface(), "iface/Compress"))
    out.update(dump_tree(UpscaleInterface(), "iface/Upscale"))
    out.update(dump_tree(SettingInterface(), "iface/Setting"))
    out.update(dump_tree(AboutInterface(), "iface/About"))
    return out


def _collect_format_cards() -> dict[str, str]:
    """采集格式卡片的常态与选中态样式。"""
    from momentshift.gui.format_grid import FormatCard

    out: dict[str, str] = {}
    card = FormatCard("video", "mp4")
    out.update(dump_tree(card, "misc/FormatCard"))
    card.set_selected(True)
    out.update(dump_tree(card, "misc/FormatCard[selected]"))
    return out


def collect_misc_widgets() -> dict[str, str]:
    """采集其余含硬编码色的独立控件。

    Notes:
        逐控件独立容错：任一控件构造失败只记录该项，不影响其余采集，
        避免一个签名问题导致整个分区快照丢失。
    """
    from momentshift.gui.advanced_panel import AdvancedPanel
    from momentshift.gui.compare_widget import CompareWidget
    from momentshift.gui.drop_area import DropArea
    from momentshift.gui.engine_card import EnginesCard
    from momentshift.gui.ffmpeg_card import FfmpegCard
    from momentshift.gui.help_bubble import HelpDialog

    items: list[tuple[str, Any]] = [
        ("misc/DropArea", lambda: dump_tree(DropArea(), "misc/DropArea")),
        ("misc/FormatCard", _collect_format_cards),
        ("misc/HelpDialog", lambda: dump_tree(HelpDialog("帮助正文"), "misc/HelpDialog")),
        ("misc/FfmpegCard", lambda: dump_tree(FfmpegCard(), "misc/FfmpegCard")),
        ("misc/EnginesCard", lambda: dump_tree(EnginesCard(), "misc/EnginesCard")),
        ("misc/CompareWidget", lambda: dump_tree(CompareWidget(), "misc/CompareWidget")),
        ("misc/AdvancedPanel", lambda: dump_tree(AdvancedPanel(), "misc/AdvancedPanel")),
    ]
    out: dict[str, str] = {}
    for label, fn in items:
        try:
            out.update(fn())
        except Exception as exc:  # noqa: BLE001 - 快照工具需报告任何采集失败
            out[f"<采集失败>/{label}"] = f"{type(exc).__name__}: {exc}"
    return out


def establish_production_theme() -> None:
    """建立与生产逐字节一致的主题态，并自检主色确实生效。

    Raises:
        SystemExit: 主题色与 :data:`momentshift.gui.tokens.ACCENT` 不符时直接退出（码 3）。

    Notes:
        为什么必须显式建立而不能「跑起来是什么就是什么」：qfluentwidgets 大量内置
        控件（HyperlinkLabel/PushButton/CheckBox…）的 QSS 由 StyleSheetManager 按
        ``themeColor()`` 现算，而 themeColor 存在 qconfig 里、由 ``core.config`` 在
        import 期从 **config.json** 载入。config.json 是 gitignore 的运行时产物，
        还会被 pytest 改写 —— 于是快照基线被悄悄绑死在「采集那台机器当时的磁盘状态」
        上，换个干净环境重跑就全红。这正是 v0.8.0-B0 修的缺陷。

        自检护栏的意义：宁可红着退出，也不要拿错误的主题态静默产出一份看似正常的
        快照 —— 那比不测更危险（会把真实回归洗白成"基线如此"）。

        这里刻意**不**调 :func:`~momentshift.app_bootstrap.create_application`：它还会
        加载随包字体、按 config.json 里的语言项切 locale，二者都是机器相关的，
        会把刚拆掉的环境耦合原样装回来。主题初始化本身走的是与生产同一个
        :func:`~momentshift.app_bootstrap.apply_theme`，视觉同源已经成立。
    """
    from qfluentwidgets import themeColor

    from momentshift.app_bootstrap import apply_theme, install_fluent_patches
    from momentshift.gui import tokens

    apply_theme()
    # 生产在 apply_theme 之后必装补丁（强制标签背景透明），不装则快照与生产不符。
    install_fluent_patches()

    actual = themeColor().name().lower()
    expected = tokens.ACCENT.lower()
    if actual != expected:
        print(
            f"[快照] 主题色自检失败：期望 {expected}（tokens.ACCENT），实际 {actual}。\n"
            f"       主题态未按生产方式建立，本次快照不可信，已中止。"
        )
        sys.stdout.flush()
        os._exit(3)
    print(f"[快照] 主题色自检通过：{actual}")


def build_snapshot() -> dict[str, str]:
    """构造 QApplication、建立生产主题态并汇总全部分区快照。"""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    assert app is not None
    establish_production_theme()

    snap: dict[str, str] = {}
    sections = (
        ("tokens", collect_tokens),
        ("theme_api", collect_theme_api),
        ("builders", collect_builders),
        ("queue", collect_queue_widgets),
        ("interfaces", collect_interfaces),
        ("misc", collect_misc_widgets),
    )
    for label, fn in sections:
        try:
            snap.update(fn())
        except Exception as exc:  # noqa: BLE001 - 快照工具需报告任何采集失败
            snap[f"<采集失败>/{label}"] = f"{type(exc).__name__}: {exc}"
    return dict(sorted(snap.items()))


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """命令行入口：``--write`` 生成基线，``--check`` 与基线比对。"""
    parser = argparse.ArgumentParser(description="QSS 视觉快照比对工具")
    parser.add_argument("--write", action="store_true", help="生成/覆盖基线文件")
    parser.add_argument("--check", action="store_true", help="与基线比对并在有差异时返回 1")
    args = parser.parse_args(argv)

    snap = build_snapshot()
    failures = [k for k in snap if k.startswith("<采集失败>")]

    if args.write:
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"[快照] 已写入基线：{BASELINE}（{len(snap)} 项）")
        for key in failures:
            print(f"  [警告] {key} -> {snap[key]}")
        return 0

    if not args.check:
        parser.error("必须指定 --write 或 --check")

    if not BASELINE.exists():
        print(f"[快照] 基线不存在：{BASELINE}")
        return 2
    base: dict[str, str] = json.loads(BASELINE.read_text(encoding="utf-8"))

    added = sorted(set(snap) - set(base))
    removed = sorted(set(base) - set(snap))
    diff_keys = sorted(k for k in set(base) & set(snap) if base[k] != snap[k])
    format_only: list[str] = []
    order_only: list[str] = []
    substantive: list[str] = []
    for key in diff_keys:
        if normalize_qss(base[key]) == normalize_qss(snap[key]):
            format_only.append(key)
        elif order_equivalent(base[key], snap[key]):
            order_only.append(key)
        else:
            substantive.append(key)

    print(f"[快照] 基线 {len(base)} 项 / 当前 {len(snap)} 项")
    print(
        f"[快照] 新增 {len(added)} / 消失 {len(removed)}"
        f" / 仅格式差异 {len(format_only)} / 仅顺序差异 {len(order_only)}"
        f" / 实质差异 {len(substantive)}"
    )
    for key in added:
        print(f"  + {key}\n      {snap[key]}")
    for key in removed:
        print(f"  - {key}\n      {base[key]}")
    for key in format_only:
        print(f"  ~ [仅空白] {key}\n      基线: {base[key]}\n      当前: {snap[key]}")
    for key in order_only:
        print(f"  ~ [仅顺序] {key}\n      基线: {base[key]}\n      当前: {snap[key]}")
    for key in substantive:
        print(f"  ! [实质] {key}\n      基线: {base[key]}\n      当前: {snap[key]}")
    for key in failures:
        print(f"  [警告] {key} -> {snap[key]}")

    ok = not (added or removed or substantive)
    print("[快照] 视觉零回归：通过" if ok else "[快照] 视觉零回归：失败")
    # 绕开 Qt 析构在沙箱内的不确定行为
    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
