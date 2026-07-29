"""Application configuration (persisted as JSON via qfluentwidgets' qconfig).

Settings are stored per-user in an OS-appropriate config directory so the app
stays portable and cross-platform aware:

- Windows : ``%APPDATA%/MomentShift/config.json``
- macOS   : ``~/Library/Application Support/MomentShift/config.json``
- Linux   : ``~/.config/MomentShift/config.json``
"""

from __future__ import annotations

import os
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


def config_dir() -> Path:
    """Return (and create) the OS-specific config directory."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    directory = base / "MomentShift"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


CONFIG_FILE = config_dir() / "config.json"


class Config(QConfig):
    """Persistent application settings."""

    # Default output folder (empty => next to source file).
    outputFolder = ConfigItem("Folder", "Output", "", FolderValidator())

    # Conversion behaviour.
    hardware = OptionsConfigItem(
        "Convert", "Hardware", "auto", OptionsValidator(["auto", "cpu", "gpu"])
    )
    maxThreads = RangeConfigItem("Convert", "MaxThreads", 4, RangeValidator(1, 16))
    ffmpegSource = OptionsConfigItem(
        "Convert", "FFmpegSource", "auto", OptionsValidator(["auto", "path"])
    )

    # UI preferences.
    language = ConfigItem("UI", "Language", "Auto")  # Auto | zh_CN | zh_TW | en_US
    theme = OptionsConfigItem(
        "UI", "Theme", "auto", OptionsValidator(["auto", "light", "dark"])
    )


cfg = Config()
qconfig.load(str(CONFIG_FILE), cfg)
