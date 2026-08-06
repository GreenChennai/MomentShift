"""硬件编码器可用性的异步探测与下拉框门禁（V0.8.21 新增）。

职责边界：
- 做：在后台线程实测本机能用哪些 GPU 编码器，把结果回灌到 UI 下拉框，
  把用不了的项置灰并标注。
- 不做：不做实际探测逻辑（那是 core/hardware 的活）；不构造 ffmpeg 命令。

依赖：core/hardware、core/ffmpeg、core/qt_compat；
被依赖：gui/compress_task_panel、gui/advanced_panel。

为什么要有这一层
----------------
V0.8.20 之前，「视频编码器」下拉框把 ``h264_nvenc`` / ``hevc_nvenc`` 静态列出来，
不问显卡。AMD / Intel 用户选了之后一路畅通无阻，直到 ffmpeg 真跑起来才炸
``Could not open encoder before EOF``，而且错误里还跟着一句误导性的
「maybe incorrect parameters such as bit_rate, rate, width or height」，
让人以为是参数填错了。

探测本身要跑 ffmpeg 子进程（每个编码器实际编一帧 nullsrc），几百毫秒起步，
放在对话框构造里会明显卡手，所以必须异步 + 缓存。
"""

from __future__ import annotations

from ..core.logger import get_logger
from ..core.qt_compat import QObject, QRunnable, QThreadPool, Signal

log = get_logger("hwprobe")

# 需要门禁的硬件编码器。CPU 编码器不用探测，永远可用。
HW_ENCODERS: tuple[str, ...] = (
    "h264_nvenc",
    "hevc_nvenc",
    "h264_qsv",
    "hevc_qsv",
    "h264_amf",
    "hevc_amf",
    "h264_videotoolbox",
    "hevc_videotoolbox",
)

# 进程内结果缓存：探测过一次就不再重复跑子进程。
# core.hardware 自己也有 _PROBE_CACHE，这里再存一层是为了让 UI 能同步拿到
# 「已经探过了」的结论而不必再进线程池绕一圈。
_RESULT: dict[str, bool] = {}


def cached_result() -> dict[str, bool]:
    """返回已探测出的 ``{编码器: 是否可用}``（可能为空）。

    Returns:
        进程内缓存的浅拷贝。尚未探测完成时返回空字典，调用方应据此
        决定是「先全部可选、稍后回灌」还是「直接应用」。
    """
    return dict(_RESULT)


class _ProbeSignals(QObject):
    """探测结果信号载体（在 GUI 线程创建，worker 线程发出）。"""

    done = Signal(dict)


class HwProbeWorker(QRunnable):
    """后台实测硬件编码器可用性的一次性 worker。

    线程约定：``run()`` 在线程池线程执行，结果只经 :attr:`signals` 回传，
    禁止在其中触碰任何 Qt 控件。
    """

    def __init__(self, encoders: tuple[str, ...] = HW_ENCODERS):
        super().__init__()
        self.setAutoDelete(True)
        self.encoders = encoders
        self.signals = _ProbeSignals()

    def run(self) -> None:
        result: dict[str, bool] = {}
        try:
            from ..core.ffmpeg import find_ffmpeg
            from ..core.hardware import encoder_usable

            exe = find_ffmpeg()
            if not exe:
                # 连 ffmpeg 都没有，谈不上门禁。全部放行，别把下拉框锁死，
                # 否则用户装好 ffmpeg 之前会以为是软件坏了。
                log.debug("未找到 ffmpeg，跳过硬件编码器探测")
                self.signals.done.emit({})
                return
            for enc in self.encoders:
                try:
                    result[enc] = bool(encoder_usable(exe, enc))
                except Exception:  # noqa: BLE001 - 单个编码器探测失败不影响其余
                    result[enc] = True  # 保守放行，交由 ffmpeg 自己报错降级
            log.info("硬件编码器探测结果：%s", {k: v for k, v in result.items() if v})
        except Exception:  # pragma: no cover - defensive
            log.exception("硬件编码器探测异常，全部放行")
            result = {}
        self.signals.done.emit(result)


def probe_async(callback) -> None:
    """异步探测硬件编码器可用性，完成后在 GUI 线程回调。

    Args:
        callback: ``cb(dict[str, bool])``。已有缓存时**同步**立即调用，
            不进线程池。
    Notes:
        多次调用只会真正探测一次，后续都走缓存。
    """
    if _RESULT:
        callback(dict(_RESULT))
        return

    worker = HwProbeWorker()

    def _on_done(result: dict):
        if result:
            _RESULT.update(result)
        callback(dict(result))

    worker.signals.done.connect(_on_done)
    QThreadPool.globalInstance().start(worker)


def apply_encoder_gate(combo, result: dict[str, bool], unavailable_suffix: str = "（本机不可用）") -> None:
    """把探测结果应用到编码器下拉框：不可用项置灰并加后缀。

    Args:
        combo: qfluentwidgets 的 ``ComboBox``，需带 ``._mapping``
            （由 ``InterfaceBase._make_combo`` 挂上，形如 ``[(显示名, 值), ...]``）。
        result: ``{编码器: 是否可用}``；空字典表示探测无结论，直接返回不动。
        unavailable_suffix: 追加到不可用项显示名后的提示。
    Notes:
        **绝不调用 ``combo.model()``**（项目既定约定），只用
        ``setItemEnabled`` 与 ``setItemText``。

        当前选中项恰好不可用时不强行改选——那会让用户莫名其妙地发现自己的
        选择被偷换。置灰 + 文案提示已经足够，真跑起来还有 ``pick_video_encoder``
        的门禁和 ``run()`` 的降级重试兜底。
    """
    if not result:
        return
    mapping = getattr(combo, "_mapping", None)
    if not mapping:
        return
    for idx, pair in enumerate(mapping):
        try:
            label, value = pair
        except (TypeError, ValueError):
            continue
        if value not in result:
            continue
        ok = result[value]
        try:
            combo.setItemEnabled(idx, ok)
            if not ok and unavailable_suffix not in label:
                combo.setItemText(idx, f"{label}{unavailable_suffix}")
        except (AttributeError, RuntimeError):
            continue  # 静默原因：控件可能已随界面销毁
