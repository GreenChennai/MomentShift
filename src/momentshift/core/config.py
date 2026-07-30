"""Application configuration (persisted as JSON via qfluentwidgets' qconfig).

The config file lives **inside the software directory** (next to the executable)
so the app stays self-contained and portable:

- Frozen exe : ``<dir of MomentShift.exe>/config/config.json``
- Dev run    : ``<repo root>/config/config.json``
"""

from __future__ import annotations

import sys
from pathlib import Path

from qfluentwidgets import (
    QConfig,
    ConfigItem,
    OptionsConfigItem,
    RangeConfigItem,
    OptionsValidator,
    RangeValidator,
    FolderValidator,
    qconfig,
)


def app_base_dir() -> Path:
    """Return the directory that should hold the config folder.

    When frozen (PyInstaller), this is the directory containing the executable.
    In development it is the repository root (the parent of ``src``).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # src/momentshift/core/config.py -> repo root is four levels up.
    return Path(__file__).resolve().parent.parent.parent.parent


def config_dir() -> Path:
    """Return the config directory inside the software folder.

    Deprecated: config now lives at the app root (see ``CONFIG_FILE``). Kept
    only to migrate an old ``config/config.json`` into the new location.
    Does NOT create the directory (avoids polluting the app root).
    """
    return app_base_dir() / "config"


def tools_dir() -> Path:
    """Unified folder for bundled external tools (compressors, upscaler, ...).

    Lives directly inside the software directory so the app can manage these
    binaries itself (a one-click in-app download drops them here), instead of
    relying on the user to place files next to the exe or on PATH.
    """
    directory = app_base_dir() / "tools"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _old_config_file() -> Path:
    """The pre-v0.1.6 location of the config file, used for one-time migration."""
    return app_base_dir() / "config" / "config.json"


CONFIG_FILE = app_base_dir() / "config.json"


class Config(QConfig):
    """Persistent application settings."""

    # Default output folder (empty => next to source file).
    outputFolder = ConfigItem("Folder", "Output", "", FolderValidator())

    # Output location strategy: "fixed" => use outputFolder; "same" => next to
    # the source file with a custom suffix appended to the stem.
    outputMode = OptionsConfigItem(
        "Folder", "OutputMode", "fixed", OptionsValidator(["fixed", "same"])
    )
    outputSuffix = ConfigItem("Folder", "OutputSuffix", "_converted")

    # Compress module — its own output location (independent of Convert).
    compressFolder = ConfigItem("Folder", "CompressFolder", "", FolderValidator())
    compressMode = OptionsConfigItem(
        "Folder", "CompressMode", "fixed", OptionsValidator(["fixed", "same"])
    )
    compressSuffix = ConfigItem("Folder", "CompressSuffix", "_compressed")

    # Upscale module — its own output location (independent of Convert).
    upscaleFolder = ConfigItem("Folder", "UpscaleFolder", "", FolderValidator())
    upscaleMode = OptionsConfigItem(
        "Folder", "UpscaleMode", "fixed", OptionsValidator(["fixed", "same"])
    )
    upscaleSuffix = ConfigItem("Folder", "UpscaleSuffix", "_upscaled")

    # Conversion behaviour.
    hardware = OptionsConfigItem(
        "Convert", "Hardware", "auto", OptionsValidator(["auto", "cpu", "gpu"])
    )
    maxThreads = RangeConfigItem("Convert", "MaxThreads", 3, RangeValidator(1, 16))
    ffmpegSource = OptionsConfigItem(
        "Convert", "FFmpegSource", "auto", OptionsValidator(["auto", "path"])
    )

    # UI preferences.
    language = ConfigItem("UI", "Language", "Auto")  # Auto | zh_CN | zh_TW | en_US
    theme = OptionsConfigItem(
        "UI", "Theme", "auto", OptionsValidator(["auto", "light", "dark"])
    )
    autoCollapse = ConfigItem("UI", "AutoCollapse", True)

    # System tray: minimise to tray on close instead of quitting (v0.2.7, #3).
    closeToTray = ConfigItem("UI", "CloseToTray", True)

    # Quick Launch (v0.2.9): Windows right-click context menu integration.
    quickLaunchEnabled = ConfigItem("QuickLaunch", "Enabled", False)
    quickLaunchBindMenu = ConfigItem("QuickLaunch", "BindMenu", True)
    quickLaunchConvert = ConfigItem("QuickLaunch", "Convert", False)
    quickLaunchCompress = ConfigItem("QuickLaunch", "Compress", False)
    quickLaunchUpscale = ConfigItem("QuickLaunch", "Upscale", False)


cfg = Config()


def _migrate_config() -> None:
    """One-time move of an old ``config/config.json`` to the app-root location."""
    old = _old_config_file()
    if not CONFIG_FILE.exists() and old.exists():
        try:
            CONFIG_FILE.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass


# Config lives at the software root (a single, self-contained settings file).
# If it is missing on first run, create it with the default values so the app
# always has a valid, populated settings file.
_migrate_config()
qconfig.load(str(CONFIG_FILE), cfg)
if not CONFIG_FILE.exists():
    qconfig.save()
