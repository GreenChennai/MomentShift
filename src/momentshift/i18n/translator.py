"""基于 JSON 的 MomentShift 国际化。

职责边界：
- 做：从 i18n/locales/<key>.json 加载语言包，提供带占位符替换与回退链的 tr()。
- 不做：不负责加载 Qt 自身的翻译；不缓存到磁盘。

依赖：core/logger；被依赖：全项目界面与 quick_runner。

为什么用 JSON 而不是 Qt Linguist：团队需要「容易编辑、容易阅读」的语言文件。
JSON 对人和 Git 都友好，且不需要编译步骤。

示例::

    from momentshift.i18n.translator import tr
    label.setText(tr("nav.convert"))

回退链：请求语言 → en_US → 原始 key，保证生产环境界面永不显示裸 key
（en_US 始终完整）。
"""

from __future__ import annotations

import json
import locale as _locale
from enum import Enum
from pathlib import Path

from ..core.logger import get_logger

log = get_logger("i18n")

LOCALE_DIR = Path(__file__).parent / "locales"


class LocaleKey(str, Enum):
    """支持的语言标识，取值与 locales/ 下的 JSON 文件名一一对应。

    继承 str 是为了让枚举成员可直接用于路径拼接与配置持久化，
    无需每次 .value。
    """

    ZH_CN = "zh_CN"
    ZH_TW = "zh_TW"
    EN_US = "en_US"
    AUTO = "Auto"


# 顺序即设置页下拉框的展示顺序，调整会直接影响界面
SUPPORTED_LOCALES = [LocaleKey.ZH_CN, LocaleKey.ZH_TW, LocaleKey.EN_US]

NATIVE_NAMES = {
    LocaleKey.ZH_CN: "简体中文",
    LocaleKey.ZH_TW: "繁體中文",
    LocaleKey.EN_US: "English",
    LocaleKey.AUTO: "跟随系统",
}


def detect_system_locale() -> LocaleKey:
    """把操作系统语言映射到本项目支持的语言之一。

    Returns:
        匹配到的 LocaleKey；无法识别时统一回退 EN_US。

    Notes:
        取不到系统语言时按 en_US 处理，而不是抛异常，
        因为语言探测失败不应该阻断启动。
    """
    name = _locale.getdefaultlocale()[0] or "en_US"
    name = name.replace("-", "_")
    if name.startswith("zh"):
        # 港澳台及 Hant 标记一律视为繁体
        if any(tag in name for tag in ("TW", "HK", "MO", "Hant")):
            return LocaleKey.ZH_TW
        return LocaleKey.ZH_CN
    return LocaleKey.EN_US


class Translator:
    """一次性加载全部语言包，并按回退链解析文案 key。

    典型用法::

        translator.set_locale(LocaleKey.EN_US)
        text = translator.get("nav.convert")

    线程约定：只在 GUI 主线程调用；语言包在构造时全部读入内存，
    切换语言不再触发磁盘 IO，因此 set_locale 不会阻塞界面。
    """

    def __init__(self) -> None:
        self._locale: LocaleKey = LocaleKey.ZH_CN
        self._data: dict[LocaleKey, dict] = {}
        self._load_all()

    def _load_all(self) -> None:
        """把全部语言包读入内存。

        Notes:
            单个语言包缺失或 JSON 损坏时降级为空字典而非抛异常，
            让回退链兜到 en_US，避免一个坏文件导致整个程序起不来。
        """
        for loc in SUPPORTED_LOCALES:
            path = LOCALE_DIR / f"{loc.value}.json"
            try:
                self._data[loc] = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._data[loc] = {}
            except json.JSONDecodeError as exc:  # pragma: no cover
                log.warning("语言包解析失败，跳过：%s（%s）", path, exc)
                self._data[loc] = {}

    # --- 语言配置 ---
    def set_locale(self, locale: LocaleKey) -> None:
        """切换当前语言，AUTO 会即时解析为具体语言。

        Args:
            locale: 目标语言；传入不受支持的值时静默忽略，保持原语言。
        """
        if locale == LocaleKey.AUTO:
            locale = detect_system_locale()
        if locale in SUPPORTED_LOCALES:
            self._locale = locale
            # 同步 Qt 原生对话框（QFileDialog、QMessageBox）的语言，
            # 否则应用界面已是中文、系统弹窗按钮却仍是英文，观感割裂
            from PyQt6.QtCore import QLocale

            _map = {
                LocaleKey.ZH_CN: QLocale(QLocale.Language.Chinese, QLocale.Country.China),
                LocaleKey.ZH_TW: QLocale(QLocale.Language.Chinese, QLocale.Country.Taiwan),
                LocaleKey.EN_US: QLocale(QLocale.Language.English, QLocale.Country.UnitedStates),
            }
            qloc = _map.get(locale)
            if qloc:
                QLocale.setDefault(qloc)

    @property
    def locale(self) -> LocaleKey:
        """当前生效的语言（AUTO 已被解析为具体语言）。"""
        return self._locale

    # --- 文案解析 ---
    def get(self, key: str, default: str | None = None, **kwargs) -> str:
        """按回退链解析文案，并做占位符替换。

        Args:
            key: 文案 key，如 ``nav.convert``。
            default: 全部回退都落空时使用的兜底文案；为 None 时返回 key 本身。
            **kwargs: 传给 str.format 的占位符实参。

        Returns:
            最终文案。占位符替换失败时返回未替换的原始文案，
            宁可界面显示 ``{name}`` 也不让界面因文案问题崩溃。
        """
        value = self._data.get(self._locale, {}).get(key)
        if value is None:
            value = self._data.get(LocaleKey.EN_US, {}).get(key)
        if value is None:
            value = default if default is not None else key
        if kwargs:
            try:
                value = value.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                log.warning("占位符缺失或类型不符，原样返回文案：%r", value)
        return value

    def __call__(self, key: str, default: str | None = None, **kwargs) -> str:
        """让实例可直接当函数调用，等价于 get()。"""
        return self.get(key, default, **kwargs)


# 全局单例：设置页修改它的语言后，再向主窗口发 retranslate 信号统一刷新界面。
# 用单例而不是依赖注入，是因为文案取用点遍布所有界面，逐层传参代价过高。
translator = Translator()


def tr(key: str, default: str | None = None, **kwargs) -> str:
    """取文案的快捷函数，等价于 ``translator.get()``。

    Args:
        key: 文案 key，如 ``nav.convert``。
        default: 兜底文案。
        **kwargs: 占位符实参。

    Returns:
        解析后的文案。
    """
    return translator(key, default, **kwargs)


def available_languages() -> list[tuple[LocaleKey, str]]:
    """列出设置页语言下拉框需要的全部选项。

    Returns:
        ``(LocaleKey, 母语名称)`` 列表，AUTO（跟随系统）固定排在首位。
    """
    return [(LocaleKey.AUTO, NATIVE_NAMES[LocaleKey.AUTO])] + [
        (loc, NATIVE_NAMES[loc]) for loc in SUPPORTED_LOCALES
    ]
