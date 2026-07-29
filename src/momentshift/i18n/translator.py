"""JSON-backed internationalization for MomentShift.

Why JSON (not Qt Linguist): the team wants language files that are *easy to edit
and read*. JSON is human-friendly, diff-friendly in Git, and requires no compiler
step. Locale files live in ``i18n/locales/<key>.json``.

Usage::

    from momentshift.i18n.translator import tr
    label.setText(tr("nav.convert"))

Fallback chain: requested locale -> ``en_US`` -> the key itself, so the UI never
shows a raw key in production (en_US is always complete).
"""

from __future__ import annotations

import json
import locale as _locale
from enum import Enum
from pathlib import Path
from typing import Optional

LOCALE_DIR = Path(__file__).parent / "locales"


class LocaleKey(str, Enum):
    """Supported locale identifiers (match the JSON file names)."""

    ZH_CN = "zh_CN"
    ZH_TW = "zh_TW"
    EN_US = "en_US"
    AUTO = "Auto"


# Order matters: shown in the settings combo.
SUPPORTED_LOCALES = [LocaleKey.ZH_CN, LocaleKey.ZH_TW, LocaleKey.EN_US]

NATIVE_NAMES = {
    LocaleKey.ZH_CN: "简体中文",
    LocaleKey.ZH_TW: "繁體中文",
    LocaleKey.EN_US: "English",
    LocaleKey.AUTO: "跟随系统",
}


def detect_system_locale() -> LocaleKey:
    """Map the OS locale to one of our supported locales."""
    name = (_locale.getdefaultlocale()[0] or "en_US")
    name = name.replace("-", "_")
    if name.startswith("zh"):
        # Traditional Chinese regions.
        if any(tag in name for tag in ("TW", "HK", "MO", "Hant")):
            return LocaleKey.ZH_TW
        return LocaleKey.ZH_CN
    return LocaleKey.EN_US


class Translator:
    """Loads all locales once and resolves keys with graceful fallback."""

    def __init__(self) -> None:
        self._locale: LocaleKey = LocaleKey.ZH_CN
        self._data: dict[LocaleKey, dict] = {}
        self._load_all()

    def _load_all(self) -> None:
        for loc in SUPPORTED_LOCALES:
            path = LOCALE_DIR / f"{loc.value}.json"
            try:
                self._data[loc] = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._data[loc] = {}
            except json.JSONDecodeError as exc:  # pragma: no cover
                print(f"[i18n] failed to parse {path}: {exc}")
                self._data[loc] = {}

    # -- configuration -----------------------------------------------------
    def set_locale(self, locale: LocaleKey) -> None:
        if locale == LocaleKey.AUTO:
            locale = detect_system_locale()
        if locale in SUPPORTED_LOCALES:
            self._locale = locale
            # Sync Qt's native dialog language (QFileDialog, QMessageBox)
            # with the app's chosen language so buttons like "打开"/"重置"
            # match the user's selection.
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
        return self._locale

    # -- resolution --------------------------------------------------------
    def get(self, key: str, default: Optional[str] = None, **kwargs) -> str:
        value = self._data.get(self._locale, {}).get(key)
        if value is None:
            value = self._data.get(LocaleKey.EN_US, {}).get(key)
        if value is None:
            value = default if default is not None else key
        if kwargs:
            try:
                value = value.format(**kwargs)
            except (KeyError, IndexError, ValueError):
                pass
        return value

    def __call__(self, key: str, default: Optional[str] = None, **kwargs) -> str:
        return self.get(key, default, **kwargs)


# Module-level singleton used across the app. The settings screen mutates its
# locale and then triggers a retranslate signal on the main window.
translator = Translator()


def tr(key: str, default: Optional[str] = None, **kwargs) -> str:
    """Convenience helper: ``tr("nav.convert")``."""
    return translator(key, default, **kwargs)


def available_languages() -> list[tuple[LocaleKey, str]]:
    """Return ``(key, native_name)`` pairs including the AUTO option."""
    return [(LocaleKey.AUTO, NATIVE_NAMES[LocaleKey.AUTO])] + [
        (loc, NATIVE_NAMES[loc]) for loc in SUPPORTED_LOCALES
    ]
