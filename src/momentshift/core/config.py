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
    """Return (and create) the config directory inside the software folder."""
    directory = app_base_dir() / "config"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


CONFIG_FILE = config_dir() / "config.json"


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
