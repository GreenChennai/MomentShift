"""音频转文字（ASR）工作线程与纯逻辑（分段 / 时长探测 / ffmpeg 命令拼装）。

职责边界：
- 做：把「视频/音频 → 文字」的整条流水线跑在后台线程：视频提取音频（或音频
  直接使用）→ 探测时长 → 按 60s 分段切临时 wav → 逐段调用
  :func:`core.asr_client.transcribe` → 汇总完整文案；全部结果经 Qt 信号回 UI。
- 不做：不构造任何控件；不持有界面引用（UI 在 ``gui/asr_interface``）。

模块顶部是**纯函数**（分段 / 时长 / 命令拼装），离屏测试只测它们，不整树构造
界面（沙箱限制：中文 ComboBox 树离屏可能 exit 127 硬杀）。

线程约定：:class:`AsrTranscribeWorker` 是 ``QThread`` 子类，``run()`` 在后台
线程执行；信号经 Qt 队列连接自动投递回 GUI 线程。停止 = 置取消标志，worker 在
检查点自行退出并清理临时目录。
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .asr_client import AsrError, transcribe
from .funasr_engine import FunasrEngineError, transcribe_local
from .platform import popen_silent, run_silent
from .presets import guess_category
from .qt_compat import QThread, Signal

# 默认分段长度（秒），与用户 FunASR 部署建议一致（120s 测试音频切 2 段）。
DEFAULT_SEGMENT_SEC = 60.0
# ffmpeg 提取/分段命令的超时（秒）；单段切分很快，给足余量防慢盘。
_FFMPEG_TIMEOUT = 180.0
# 取消时留给 ffmpeg 收尾的宽限期（秒），同 converter._TERMINATE_GRACE_SEC。
_TERMINATE_GRACE_SEC = 2.0


# =============================================================================
# 纯函数（可离屏单测）
# =============================================================================
def segment_count(duration_sec: float, segment_sec: float = DEFAULT_SEGMENT_SEC) -> int:
    """按段长切分需要的段数（至少 1 段）。

    Args:
        duration_sec: 总时长（秒）。
        segment_sec: 每段时长（秒）。
    Returns:
        段数；``duration_sec <= 0`` 或 ``segment_sec <= 0`` 时返回 1（整段）。
    """
    if duration_sec <= 0 or segment_sec <= 0:
        return 1
    return max(1, math.ceil(duration_sec / segment_sec))


def segment_ranges(
    duration_sec: float, segment_sec: float = DEFAULT_SEGMENT_SEC
) -> list[tuple[float, float]]:
    """按段长切分，返回各段的 ``(起, 止)`` 时间范围（秒）。

    Returns:
        ``[(0, 60), (60, 120), (120, 125)]`` 这类列表；时长未知/非正时返回
        ``[(0.0, 0.0)]``（哨兵值：整段，调用方不再切分）。
    """
    if duration_sec <= 0 or segment_sec <= 0:
        return [(0.0, 0.0)]
    count = segment_count(duration_sec, segment_sec)
    return [
        (i * segment_sec, min((i + 1) * segment_sec, duration_sec)) for i in range(count)
    ]


def format_timestamp(seconds: float) -> str:
    """秒 → ``MM:SS``（不含方括号）。"""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def format_segment_marker(start: float, end: float) -> str:
    """段范围 → ``[00:00-01:00]`` 标记；end<=start 表示整段未知时长 → ``[00:00-…]``。"""
    if end > start:
        return f"[{format_timestamp(start)}-{format_timestamp(end)}]"
    return f"[{format_timestamp(start)}-…]"


def build_extract_audio_cmd(ffmpeg_path: str, input_path: str, output_wav: str) -> list[str]:
    """视频 → 16k 单声道 wav 的 ffmpeg 参数（不含二进制）。

    与用户 FunASR 部署的输入约定一致：``-vn -ac 1 -ar 16000`` +
    ``pcm_s16le``。音频文件走同一命令即为「重编码归一化」。
    """
    return [
        "-hide_banner",
        "-nostats",
        "-progress",
        "pipe:1",
        "-y",
        "-i",
        input_path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        output_wav,
    ]


def build_segment_cut_cmd(
    ffmpeg_path: str, source: str, out_wav: str, start_sec: float, dur_sec: float
) -> list[str]:
    """从源音频切出 ``[start, start+dur]`` 一段 16k 单声道 wav。

    ``-ss`` 放在 ``-i`` 之前启用快速 seek；输出统一重编码为 pcm_s16le 16k，
    保证 ASR 服务拿到的都是同一规格。
    """
    return [
        "-hide_banner",
        "-nostats",
        "-y",
        "-ss",
        f"{start_sec:.3f}",
        "-t",
        f"{dur_sec:.3f}",
        "-i",
        source,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        out_wav,
    ]


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration(ffprobe_path: str | None, media_path: str, timeout: float = 30.0) -> float | None:
    """用 ffprobe 探测媒体时长（秒）。

    Returns:
        时长秒数；ffprobe 缺失、超时或输出无法解析时返回 ``None``。
    """
    if not ffprobe_path:
        return None
    try:
        proc = run_silent(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                media_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        line = (proc.stdout or "").strip()
        if line:
            return float(line)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def probe_duration_ffmpeg(ffmpeg_path: str | None, media_path: str, timeout: float = 30.0) -> float | None:
    """用 ``ffmpeg -i`` 的输出解析时长（ffprobe 缺失时的兜底）。

    Returns:
        时长秒数；解析失败返回 ``None``。
    """
    if not ffmpeg_path:
        return None
    try:
        proc = run_silent(
            [ffmpeg_path, "-hide_banner", "-i", media_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        match = _DURATION_RE.search(proc.stderr or "")
        if match:
            hours, minutes, secs = match.groups()
            return int(hours) * 3600 + int(minutes) * 60 + float(secs)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def resolve_duration(ffmpeg_path: str | None, ffprobe_path: str | None, media_path: str) -> float | None:
    """探测媒体时长：优先 ffprobe，失败回退 ffmpeg -i 解析。"""
    duration = probe_duration(ffprobe_path, media_path)
    if duration is None:
        duration = probe_duration_ffmpeg(ffmpeg_path, media_path)
    return duration


def is_video_media(path: str) -> bool:
    """按扩展名判断是否为视频文件（音频/图片/未知一律 False）。"""
    return guess_category(path) == "video"


def needs_audio_normalization(path: str) -> bool:
    """是否需要先重编码成 16k 单声道 wav。

    - 视频：必须提取音频 → True
    - 非 wav 音频：重编码归一化更稳（mp3/flac/m4a 交给 ffmpeg 解码）→ True
    - wav 音频：直接转写（尊重「拖音频直接转写」的语义）→ False
    """
    if is_video_media(path):
        return True
    return Path(path).suffix.lower() != ".wav"


# =============================================================================
# QThread worker
# =============================================================================
class AsrCancelled(Exception):
    """用户点击停止。"""


class AsrTranscribeWorker(QThread):
    """把「文件 → 完整文案」的 ASR 流水线跑在后台线程。

    Args:
        input_path: 视频或音频文件路径。
        ffmpeg_path: ffmpeg 可执行文件路径（提取/分段用）。
        ffprobe_path: ffprobe 可执行文件路径（时长探测用，可为 None）。
        mode: ``"local"`` 用内置 FunASR 本地推理；``"http"`` 走 OpenAI 兼容服务。
        base_url: ASR 服务地址（HTTP 模式；形如 ``http://127.0.0.1:8000/v1``）。
        model: 服务端模型名（HTTP 模式）。
        model_id: 本地模型清单里的 id（local 模式）。
        api_key: 可选鉴权 key（HTTP 模式）；为空则不带头。
        segment_sec: 分段长度（秒）。

    信号（全部从 worker 线程发出，GUI 线程接收）：
    - ``logMessage(str)`` —— 主 CMD 面板的状态行。
    - ``serviceLog(str)`` —— 服务模式 / 本地推理日志（请求/响应/错误）。
    - ``progressChanged(int)`` —— 0..100 整体进度。
    - ``segmentReady(int, int, str, str)`` —— ``(第几段, 总段数, 段标记, 该段文案)``。
    - ``succeeded(str)`` —— 全部完成的完整文案。
    - ``failed(str)`` —— 错误消息（含用户取消）。

    停止：调用 :meth:`request_stop`，worker 在下一个检查点退出并清理临时文件。
    """

    logMessage = Signal(str)
    serviceLog = Signal(str)
    progressChanged = Signal(int)
    segmentReady = Signal(int, int, str, str)
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        input_path: str,
        ffmpeg_path: str,
        ffprobe_path: str | None,
        base_url: str = "",
        model: str = "",
        api_key: str = "",
        segment_sec: float = DEFAULT_SEGMENT_SEC,
        mode: str = "http",
        model_id: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._input_path = input_path
        self._ffmpeg_path = ffmpeg_path or ""
        self._ffprobe_path = ffprobe_path
        self._base_url = base_url
        self._model = model
        self._api_key = api_key
        self._mode = mode if mode == "local" else "http"
        self._model_id = model_id
        self._segment_sec = float(segment_sec)
        self._cancel = threading.Event()

    # -- 外部控制 --
    def request_stop(self) -> None:
        """请求停止：置取消标志，worker 会在检查点自行退出。"""
        self._cancel.set()

    # -- 主流程 --
    def run(self) -> None:
        tmp_dir: str | None = None
        try:
            tmp_dir = tempfile.mkdtemp(prefix="momentshift_asr_")
            tmp = Path(tmp_dir)
            src = Path(self._input_path)
            if not src.is_file():
                raise AsrError(f"文件不存在：{self._input_path}")

            # ① 准备 16k 单声道 wav：视频提取音频；非 wav 音频重编码归一化；
            #    wav 音频直接使用（尊重「拖音频直接转写」语义）。
            wav_path = str(src)
            if needs_audio_normalization(str(src)):
                out_wav = str(tmp / "audio.wav")
                self.logMessage.emit("正在提取音频…")
                self._extract_audio(out_wav)
                wav_path = out_wav

            # ② 探测时长
            duration = resolve_duration(self._ffmpeg_path, self._ffprobe_path, wav_path)
            if duration is None or duration <= 0:
                ranges = [(0.0, 0.0)]  # 整段
                self.logMessage.emit("无法探测时长，按整段转写")
            else:
                ranges = segment_ranges(duration, self._segment_sec)
                self.logMessage.emit(f"音频时长 {format_timestamp(duration)}，共 {len(ranges)} 段")

            # ③④ 逐段切分 + 转写
            total = len(ranges)
            texts: list[str] = []
            for index, (start, end) in enumerate(ranges, start=1):
                self._check_cancel()
                if end > start:
                    seg_wav = str(tmp / f"seg_{index:03d}.wav")
                    self._cut_segment(wav_path, seg_wav, start, end - start)
                    self._check_cancel()
                else:
                    seg_wav = wav_path  # 整段：直接用准备好的音频

                self.logMessage.emit(f"正在转写第 {index}/{total} 段…")
                if self._mode == "local":
                    self.serviceLog.emit(
                        f"→ 本地模型 {self._model_id} 推理（{Path(seg_wav).name}）"
                    )
                else:
                    self.serviceLog.emit(
                        f"→ POST {self._base_url.rstrip('/')}/audio/transcriptions"
                        f"（{Path(seg_wav).name}）"
                    )
                t0 = time.monotonic()
                try:
                    if self._mode == "local":
                        text = transcribe_local(seg_wav, self._model_id)
                    else:
                        text = transcribe(
                            self._base_url, self._model, self._api_key, seg_wav
                        )
                except (AsrError, FunasrEngineError) as exc:
                    self.serviceLog.emit(f"← 失败：{exc}")
                    raise
                elapsed = time.monotonic() - t0
                self.serviceLog.emit(f"← 完成（{elapsed:.1f}s）")

                texts.append(text)
                marker = format_segment_marker(start, end)
                self.segmentReady.emit(index, total, marker, text)
                self.progressChanged.emit(35 + int(65 * index / total))

            # ⑤ 全部完成
            full_text = "\n".join(texts).strip()
            self.succeeded.emit(full_text)
        except AsrCancelled:
            self.failed.emit("已取消")
        except AsrError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - worker 线程兜底，异常必须转成消息
            self.failed.emit(f"内部错误：{exc}")
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # -- 内部工具 --
    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise AsrCancelled()

    def _stop_ffmpeg(self, proc: subprocess.Popen) -> None:
        """尽快结束 ffmpeg 子进程（terminate → 宽限 → kill），不抛异常。"""
        try:
            proc.terminate()
            try:
                proc.wait(timeout=_TERMINATE_GRACE_SEC)
                return
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
        except (OSError, subprocess.SubprocessError):
            pass

    def _extract_audio(self, out_wav: str) -> None:
        """执行提取音频，并用 ``-progress pipe:1`` 解析进度（0..35%）。"""
        cmd = [self._ffmpeg_path, *build_extract_audio_cmd(self._ffmpeg_path, self._input_path, out_wav)]
        duration = resolve_duration(self._ffmpeg_path, self._ffprobe_path, self._input_path)
        try:
            proc = popen_silent(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise AsrError(f"无法启动 ffmpeg：{exc}") from exc

        with proc:
            try:
                while True:
                    if self._cancel.is_set():
                        self._stop_ffmpeg(proc)
                        raise AsrCancelled()
                    line = proc.stdout.readline()
                    if not line:
                        break
                    line = line.strip()
                    if "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip()
                    pct = None
                    if key == "out_time_us" and val.lstrip("-").isdigit():
                        us = int(val)
                        if duration and us > 0:
                            pct = min(1.0, us / 1_000_000 / duration)
                    elif key == "out_time_ms" and val.lstrip("-").isdigit():
                        ms = int(val)
                        if duration and ms > 0:
                            pct = min(1.0, ms / 1000 / duration)
                    elif key == "progress" and val == "end":
                        pct = 1.0
                    if pct is not None:
                        self.progressChanged.emit(int(35 * pct))
                        self.logMessage.emit(f"正在提取音频… {int(pct * 100)}%")
            except AsrCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - 读管道异常也按失败上报
                self._stop_ffmpeg(proc)
                raise AsrError(f"读取 ffmpeg 输出失败：{exc}") from exc

            returncode = proc.wait()
        if returncode != 0:
            raise AsrError(f"音频提取失败（ffmpeg 退出码 {returncode}）")

    def _cut_segment(self, source: str, out_wav: str, start_sec: float, dur_sec: float) -> None:
        """切一段临时 wav（静默执行，快，无需进度）。"""
        cmd = [
            self._ffmpeg_path,
            *build_segment_cut_cmd(self._ffmpeg_path, source, out_wav, start_sec, dur_sec),
        ]
        try:
            proc = run_silent(cmd, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            raise AsrError(f"音频分段失败：{exc}") from exc
        if proc.returncode != 0:
            raise AsrError(f"音频分段失败（ffmpeg 退出码 {proc.returncode}）")
