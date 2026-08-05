"""应用配置（通过 qfluentwidgets 的 qconfig 持久化为 JSON）。

职责边界：
- 做：定义配置项、提供配置读写与默认值回退、一次性迁移旧路径配置。
- 不做：不直接创建 Qt 控件；不负责配置项的 UI 绑定（由各界面完成）。

依赖：core/platform（目录解析）；被依赖：全项目各模块。

配置文件位于软件目录内（贴近可执行文件），保证程序自包含、可便携：
- 打包 exe：``<MomentShift.exe 所在目录>/config/config.json``
- 开发运行：``<仓库根>/config/config.json``
"""

from __future__ import annotations

from pathlib import Path

from qfluentwidgets import (
    ConfigItem,
    FolderValidator,
    OptionsConfigItem,
    OptionsValidator,
    QConfig,
    RangeConfigItem,
    RangeValidator,
    qconfig,
)

from .asr_client import DEFAULT_BASE_URL as _DEFAULT_ASR_BASE_URL
from .asr_client import DEFAULT_MODEL as _DEFAULT_ASR_MODEL
from .platform import app_base_dir, config_file

# 目录解析统一下沉到 core.platform。这里保留 ``app_base_dir`` /
# ``tools_dir`` 的再导出，是因为 gui 层历史上从 core.config 取这两个名字，
# 全部改调用点会让本次重构的 diff 无谓地变大。
from .platform import tools_dir as tools_dir  # noqa: PLC0414


def config_dir() -> Path:
    """返回软件目录下的旧配置目录。

    Notes:
        已废弃：配置现在直接落在应用根目录（见 :data:`CONFIG_FILE`）。此函数只
        为把旧的 ``config/config.json`` 迁移到新位置而保留。刻意**不创建**目录，
        避免在应用根目录留下空文件夹。
    """
    return app_base_dir() / "config"


def _old_config_file() -> Path:
    """返回配置文件的旧存放位置，仅用于一次性迁移。"""
    return app_base_dir() / "config" / "config.json"


CONFIG_FILE = config_file()


class Config(QConfig):
    """应用的持久化设置项集合。

    典型用法::

        from momentshift.core.config import cfg

        cfg.maxThreads.value = 4   # 存盘由 connect_autosave() 自动兜底

    线程约定：仅 GUI 线程读写。工作线程需要参数时应在入队时取快照。

    Notes:
        v0.8.0 ODD-22：**业务代码不要再手写** ``qconfig.save()``。所有配置项的
        ``valueChanged`` 在应用启动时由 :func:`connect_autosave` 统一接到存盘，
        改值即落盘。历史上「集中式 + 20 多处散落调用」的双保险会让同一次改值写两
        遍磁盘，且散落调用漏加时表现为「这一项重启就丢」，很难查。
    """

    # 默认输出目录（留空表示「输出到源文件旁」）
    outputFolder = ConfigItem("Folder", "Output", "", FolderValidator())

    # 输出位置策略："fixed" 用 outputFolder；"same" 输出到源文件旁并在主名后
    # 追加自定义后缀。默认取 "same"，避免用户没设过目录时文件散落到未知位置。
    outputMode = OptionsConfigItem(
        "Folder", "OutputMode", "same", OptionsValidator(["fixed", "same"])
    )
    outputSuffix = ConfigItem("Folder", "OutputSuffix", "_converted")

    # 压缩模块：独立于「转换」的一套输出位置配置
    compressFolder = ConfigItem("Folder", "CompressFolder", "", FolderValidator())
    compressMode = OptionsConfigItem(
        "Folder", "CompressMode", "same", OptionsValidator(["fixed", "same"])
    )
    compressSuffix = ConfigItem("Folder", "CompressSuffix", "_compressed")

    # 放大模块：独立于「转换」的一套输出位置配置
    upscaleFolder = ConfigItem("Folder", "UpscaleFolder", "", FolderValidator())
    upscaleMode = OptionsConfigItem(
        "Folder", "UpscaleMode", "same", OptionsValidator(["fixed", "same"])
    )
    upscaleSuffix = ConfigItem("Folder", "UpscaleSuffix", "_upscaled")

    # 转换行为
    hardware = OptionsConfigItem(
        "Convert", "Hardware", "auto", OptionsValidator(["auto", "cpu", "gpu"])
    )
    maxThreads = RangeConfigItem("Convert", "MaxThreads", 3, RangeValidator(1, 16))
    ffmpegSource = OptionsConfigItem(
        "Convert", "FFmpegSource", "auto", OptionsValidator(["auto", "path"])
    )

    # 界面偏好
    language = ConfigItem("UI", "Language", "Auto")  # 取值：Auto | zh_CN | zh_TW | en_US

    # 系统托盘：关闭窗口时最小化到托盘而不是退出进程
    closeToTray = ConfigItem("UI", "CloseToTray", True)

    # 快速调用：Windows 右键菜单集成，默认全关，由用户显式开启
    quickLaunchEnabled = ConfigItem("QuickLaunch", "Enabled", False)
    quickLaunchBindMenu = ConfigItem("QuickLaunch", "BindMenu", True)
    quickLaunchConvert = ConfigItem("QuickLaunch", "Convert", False)
    quickLaunchCompress = ConfigItem("QuickLaunch", "Compress", False)
    quickLaunchUpscale = ConfigItem("QuickLaunch", "Upscale", False)
    # 快速调用通知开关（开始 / 完成 两个独立开关，默认都开）
    quickNotifyStart = ConfigItem("QuickLaunch", "NotifyStart", True)
    quickNotifyDone = ConfigItem("QuickLaunch", "NotifyDone", True)

    # 音频转文字（ASR）：FunASR OpenAI 兼容 HTTP 服务的三件套（v0.8.3）
    # 默认值即「零配置连本地已部署的服务」；界面上另有「启用服务模式」开关，
    # 关闭时使用内置默认地址/模型，开启后使用下面这三项。
    asrBaseUrl = ConfigItem("Asr", "BaseUrl", _DEFAULT_ASR_BASE_URL)
    asrModel = ConfigItem("Asr", "Model", _DEFAULT_ASR_MODEL)
    asrApiKey = ConfigItem("Asr", "ApiKey", "")

    # 音频转文字（ASR）本地推理参数（v0.8.5「ASR 设置」卡片）
    # - asrModelId：用于推理的本地模型清单 id；空串 = 自动（第一个已就绪模型，
    #   与 v0.8.4 的 find_ready_model() 语义一致）。
    # - asrSegmentSec：过长音频每段长度（秒），15..300，默认 60。
    # - asrStructured：结构化输出开关（VAD 时间戳 + 说话人标签，默认关）。
    # - asrDevice：推理设备策略；"auto" = 按硬件检测（N 卡+CUDA→cuda，否则 cpu）。
    asrModelId = ConfigItem("Asr", "ModelId", "")
    asrSegmentSec = RangeConfigItem("Asr", "SegmentSec", 180, RangeValidator(15, 300))
    asrStructured = ConfigItem("Asr", "Structured", False)
    asrDevice = OptionsConfigItem(
        "Asr", "Device", "auto", OptionsValidator(["auto", "cpu", "cuda"])
    )
    # v0.8.11：标点恢复（需下载 ct-punc 模型；启用后对转写结果加标点，CPU 可用）
    asrPunc = ConfigItem("Asr", "Punc", False)
    # v0.8.14 #2：情感识别调用开关（需下载 emotion2vec+large 模型 + NVIDIA CUDA
    # + 完整 funasr 包；开启后逐段转写结果附带情感标签，无模型/无硬件时不调用）
    asrEmotion = ConfigItem("Asr", "Emotion", False)

    # v0.8.9：本地服务端模式（本软件作为服务器供其他应用调用）
    # - asrServerPort：监听端口，默认 8000（对齐用户 C:\FunASR\server.py 习惯）。
    # - asrServerModel：服务端推理用的模型 id（asr 类且 engine=True）。
    asrServerPort = RangeConfigItem("Asr", "ServerPort", 8000, RangeValidator(1024, 65535))
    asrServerModel = ConfigItem("Asr", "ServerModel", "paraformer-large")


cfg = Config()


# ---------------------------------------------------------------------------
# 配置自动存盘（ODD-22 的单一策略）
# ---------------------------------------------------------------------------
_autosave_connected: list[str] = []


def _autosave() -> None:
    """任一配置项变更后落盘。

    Notes:
        必须是模块级具名函数而非 lambda：``valueChanged`` 只持有弱引用语义的槽
        包装，用局部 lambda 连接会在函数返回后被回收，表现为「有时能存有时不能」。
    """
    qconfig.save()


def iter_config_items() -> list[tuple[str, ConfigItem]]:
    """枚举 :class:`Config` 上的全部配置项。

    Returns:
        ``[(属性名, ConfigItem), ...]``，按属性名排序。含 ``QConfig`` 基类自带的
        ``themeMode`` / ``themeColor``，也含 ``OptionsConfigItem`` /
        ``RangeConfigItem`` 这类子类（它们都是 ``ConfigItem`` 的子类）。

    Notes:
        走 ``dir(Config)`` 而不是 ``vars(Config)``，否则会漏掉基类继承来的项。
    """
    items: list[tuple[str, ConfigItem]] = []
    for name in dir(Config):
        attr = getattr(Config, name, None)
        if isinstance(attr, ConfigItem):
            items.append((name, attr))
    return sorted(items, key=lambda kv: kv[0])


def connect_autosave() -> list[str]:
    """把全部配置项的 ``valueChanged`` 统一接到存盘，返回被接管的配置项名。

    Returns:
        被接管的配置项属性名列表（已排序）。重复调用不会重复连接。

    Notes:
        这是 v0.8.0 起**唯一**的配置持久化入口，由 ``gui/main_window`` 在启动时
        调用一次。返回值不是装饰用的——``tests/config_coverage.py`` 会拿它跟
        :func:`iter_config_items` 做全等断言，确保没有任何配置项漏接。
    """
    if _autosave_connected:
        return list(_autosave_connected)
    for name, item in iter_config_items():
        item.valueChanged.connect(_autosave)
        _autosave_connected.append(name)
    return list(_autosave_connected)


def ensure_config_file() -> Path:
    """保证磁盘上存在配置文件，返回其路径。

    Returns:
        配置文件路径（调用后必定存在，除非磁盘写入失败）。

    Notes:
        给「要把配置文件交给外部程序打开」的场景用（设置页的「打开配置文件」）。
        正常流程下文件在模块导入时就建好了，但用户可能在运行期把它删掉，那时
        直接 openUrl 会弹「文件不存在」。

        这里刻意不叫 ``save()``：ODD-22 之后 GUI 层不该出现任何「主动存盘」的
        语义，只有「确保文件在」这一种正当理由。
    """
    if not CONFIG_FILE.exists():
        qconfig.save()
    return CONFIG_FILE


def _migrate_config() -> None:
    """把旧的 ``config/config.json`` 一次性迁移到应用根目录的新位置。

    Notes:
        只在新位置**不存在**且旧文件存在时才搬运，避免覆盖用户的现有配置。
    """
    old = _old_config_file()
    if not CONFIG_FILE.exists() and old.exists():
        try:
            CONFIG_FILE.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass  # 静默原因：配置回退写入失败属非致命，保留原文件即可


# 配置固定放在软件根目录，保证「一个目录即完整应用」可便携拷贝。
# 首次运行若文件缺失，立刻用默认值写一份，避免后续各处都要处理「配置不存在」。
_migrate_config()
qconfig.load(str(CONFIG_FILE), cfg)
ensure_config_file()
