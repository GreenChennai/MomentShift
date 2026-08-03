"""QApplication 的统一引导：HiDPI、字体、主题、语言、qfluentwidgets 补丁。

职责边界：
- 做：把 ``__main__`` 与 ``quick_runner`` 两条启动路径的初始化步骤收敛到一处。
- 不做：不创建业务窗口、不解析命令行、不启动事件循环。

依赖：PyQt6、qfluentwidgets、core.config、i18n.translator；
被依赖：``__main__.py``、``quick_runner.py``。

历史背景：v0.8.0 之前两条启动路径各写一份字体/主题/语言初始化，
已经出现细节漂移（quick 路径多了 ``setQuitOnLastWindowClosed(False)``、
少了 ``AA_UseHighDpiPixmaps``），字体没加载会导致中文回退到系统默认字体。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QColor, QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication
from qfluentwidgets import Theme, setTheme, setThemeColor

from .core.logger import get_logger
from .core.qt_compat import Qt
from .gui import tokens
from .metadata import APP_NAME, VERSION

log = get_logger("bootstrap")

# 品牌主色（GitHub 绿）。直接引用视觉令牌，改 tokens.ACCENT 即全局生效
# （从前这里与 gui/theme.py 各写一份同值字面量，靠注释约定同步）。
THEME_COLOR = tokens.ACCENT

# 随包分发的字体：简中用 HarmonyOS Sans SC，英文/数字回退 FiraCode。
_FONT_FILES = ("HarmonyOS_Sans_SC_Regular.ttf", "FiraCode-Regular.ttf")

# 字体是否已注册。create_application 可能被调用两次（主程序 + quick 路径），
# 重复 addApplicationFont 会在 Qt 内部留下多份副本。
_fonts_loaded = False

# 全局字体族 QSS。必须用 ``*`` 通配，否则 qfluentwidgets 内部控件会各自设字体。
# v0.8.1：FiraCode-Regular.ttf 注册的族名是 ``Fira Code``（带空格），此前写
# ``FiraCode`` 与字体实际族名不符，字体即使加载也不会被用上，已一并修正。
_FONT_QSS = "* { font-family: 'HarmonyOS Sans SC', 'Fira Code', 'Microsoft YaHei', sans-serif; }"

_patches_installed = False


def load_app_fonts() -> None:
    """把随包字体注册进 Qt 字体库。幂等；缺字体只降级不报错。

    Notes:
        v0.8.1 修复：字体源文件此前只放在仓库根 ``resources/``，而这里找的是
        包内 ``src/momentshift/resources/``，且 ``build.spec`` 的 datas 没有收集
        ttf —— 字体从未真正加载，界面一直靠 QSS 里的 ``Microsoft YaHei`` 兜底。
        现已把两个 ttf 复制进包内资源目录并补上打包收集（见 build.spec）。
        缺字体（如开发机恰好没有）时继续沿用系统字体兜底，不阻断启动。
    """
    global _fonts_loaded
    if _fonts_loaded:
        return
    _fonts_loaded = True

    res = Path(__file__).resolve().parent / "resources"
    for name in _FONT_FILES:
        path = res / name
        if not path.exists():
            log.debug("内置字体未随包分发，沿用系统字体兜底：%s", name)
            continue
        fid = QFontDatabase.addApplicationFont(str(path))
        if fid < 0:
            log.warning("字体加载失败（Qt 拒绝）：%s", name)
        else:
            log.info("字体已加载：%s (id=%s)", name, fid)


def apply_locale_from_config() -> None:
    """按配置里的语言项切换界面语言。

    Notes:
        延迟导入 translator 与 cfg：两者在 import 期会读磁盘，
        放到函数里可以让本模块被 import 时保持零副作用。
    """
    from .core.config import cfg
    from .i18n.translator import LocaleKey, translator

    try:
        translator.set_locale(LocaleKey(cfg.language.value))
    except ValueError:
        # 配置里存了一个已经不存在的语言代码（手工改坏 config.json）。
        log.warning("配置中的语言代码无效：%r，回退到跟随系统", cfg.language.value)
        translator.set_locale(LocaleKey.AUTO)


def install_fluent_patches() -> None:
    """显式安装 qfluentwidgets 兼容补丁。幂等，可安全重复调用。

    Notes:
        补丁内容见 ``gui.theme``：强制 FluentLabelBase 与 SwitchButton 的
        标签背景透明，否则父控件的背景色会透过标签形成色块。
        原先写在 ``theme.py`` 的 import 期执行，导致 import 顺序敏感、
        无法关闭也无法单测；现在由启动引导在建 QApplication 后统一调用。
    """
    global _patches_installed
    if _patches_installed:
        return
    from .gui.theme import apply_fluent_patches

    apply_fluent_patches()
    _patches_installed = True
    log.info("qfluentwidgets 兼容补丁已安装")


def apply_theme() -> None:
    """建立主题态：固定浅色 + 品牌主色 :data:`THEME_COLOR`。幂等。

    Notes:
        **快照必须与生产同源调用本函数，否则基线不可复现。**
        ``tests/qss_snapshot.py`` 依赖本函数建立与生产逐字节一致的主题态；
        任何「只在生产侧改主题初始化」的改动都会让快照基线失效，请改这里。

        为什么开头要 import ``core.config``：qfluentwidgets 把 themeColor 存在
        ``qconfig`` 里，而 ``core.config`` 在 **import 期**执行
        ``qconfig.load(config.json)`` —— 磁盘上那份 ``ThemeColor`` 会**覆盖**本函数
        设过的值。历史顺序是「先 setThemeColor，再由 apply_locale_from_config
        触发 core.config 首次 import」，于是主色被上次落盘的旧值静默顶掉。
        主程序侥幸没发作，只因 ``__main__`` 顶部 import 了 ``core.queue``（间接
        import ``core.config``），load 恰好跑在前面 —— 纯属 import 顺序运气。
        这里显式先 load 再设色，把顺序钉死，任何入口都不会再踩。

        ``save=False``：主色由 :data:`THEME_COLOR` 单点决定，不是用户可调项。
        写回 config.json 只会让「磁盘旧值」与「代码新值」两个事实源互相打架
        （正是上面那个 bug 的燃料），也会污染工作区。
    """
    from .core import config as _config  # noqa: F401 - 仅为触发 qconfig.load

    setTheme(Theme.LIGHT, save=False)
    setThemeColor(QColor(THEME_COLOR), save=False)


def configure_hidpi() -> None:
    """设置 HiDPI 缩放策略。必须在 QApplication 实例化**之前**调用。"""
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    # AA_UseHighDpiPixmaps 在 Qt6 已移除（高 DPI 位图是默认行为），
    # 但部分 PyQt6 小版本仍保留该枚举，存在才设置以兼容旧环境。
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)


def create_application(
    argv: list[str],
    *,
    quick_mode: bool = False,
    app_cls: type[QApplication] | None = None,
) -> QApplication:
    """创建并完整初始化 QApplication。``__main__`` 与 ``quick_runner`` 的唯一入口。

    Args:
        argv: 命令行参数，通常是 ``sys.argv``。
        quick_mode: 右键快速调用模式。此模式下关闭"最后一个窗口关闭即退出"，
            否则设置弹窗 accept 之后进程会立刻退出，任务还没开始就没了。
        app_cls: 可选的 QApplication 子类（主程序用带异常兜底的 Application）。
    Returns:
        已完成 HiDPI、字体、主题、语言、补丁初始化的 QApplication。
    Notes:
        已存在实例时直接复用，不会重复构造（多次调用是安全的）。
    """
    configure_hidpi()

    app = QApplication.instance()
    if app is None:
        app = (app_cls or QApplication)(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    if quick_mode:
        app.setQuitOnLastWindowClosed(False)

    load_app_fonts()
    font = QFont("HarmonyOS Sans SC", 10)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(font)
    app.setStyleSheet(_FONT_QSS)

    # 应用固定为浅色主题（深色主题已于  下线，相关分支已全部移除）。
    # 与 tests/qss_snapshot.py 共用同一个函数，避免快照与生产二次分叉。
    apply_theme()

    install_fluent_patches()
    apply_locale_from_config()
    return app
