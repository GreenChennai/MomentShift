"""ffmpeg 真实进度解析（V0.8.21 新增）。

职责边界：
- 做：解析 ffmpeg ``-progress pipe:1`` 的 key=value 流与输出里的 ``Duration:``
  横幅，算出百分比 / 编码速度 / 预计剩余时间；用 ffprobe 预取媒体总时长。
- 不做：不构造 ffmpeg 命令行（见 core/converter、core/ffmpeg_compress）；
  不碰 Qt，纯逻辑，可在 worker 线程里安全调用。

依赖：core/platform、core/ffmpeg、core/config；
被依赖：core/converter、core/ffmpeg_compress、core/queue。

为什么需要这个模块
------------------
V0.8.20 之前，``converter.run_conversion`` 与 ``ffmpeg_compress._execute``
各自内联了一段进度解析，且**两处都在等一个 ffmpeg 从不输出的键**::

    if key == "duration_ms":      # ffmpeg 永远不发这个键
        duration_ms = int(val)
    elif key == "out_time_ms" and duration_ms:   # 于是这里永远进不来
        ...

结果分母恒为 None，中间进度一次都不上报，只有跑完时发一个 100。项目因此不得不
写了一整套 ``fake_progress`` 来糊住空白。本模块把解析收敛到一处并修正键名。

ffmpeg ``-progress`` 的三个坑
-----------------------------
1. **它不输出总时长。** ``-progress`` 只吐已编码位置，分母必须外部取
   （ffprobe 预取，或从 ``Duration:`` 横幅兜底）。
2. **``out_time_ms`` 的单位其实是微秒。** 这是 ffmpeg 的历史 bug，为兼容一直
   没改；后来新增的 ``out_time_us`` 才名副其实。当毫秒用会差 1000 倍。
3. **``speed`` 可能是 ``N/A``。** 刚启动的头几帧还没算出速度，直接
   ``float()`` 会抛异常。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .logger import get_logger
from .platform import run_silent

log = get_logger("progress")

# "Duration: 00:01:23.45, start: 0.000000, bitrate: 1234 kb/s"
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})\.(\d+)")
# "speed=1.53x" / "speed=0.0421x" / "speed=N/A"
_SPEED_RE = re.compile(r"^(\d+(?:\.\d+)?)x?$")
# "00:01:23.456789"
_HHMMSS_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})(?:\.(\d+))?$")


def parse_hhmmss(text: str) -> int | None:
    """把 ``HH:MM:SS.ffffff`` 解析成毫秒。

    Args:
        text: 时间字符串，小数部分位数不限（ffmpeg 有时给 2 位有时给 6 位）。
    Returns:
        毫秒整数；格式不匹配返回 ``None``。
    """
    m = _HHMMSS_RE.match((text or "").strip())
    if not m:
        return None
    h, mi, s, frac = m.groups()
    ms = (int(h) * 3600 + int(mi) * 60 + int(s)) * 1000
    if frac:
        # 小数位数不固定：补零/截断到 3 位当毫秒
        ms += int((frac + "000")[:3])
    return ms


def parse_banner_duration(line: str) -> int | None:
    """从 ffmpeg 打印的 ``Duration:`` 横幅里抓总时长（毫秒）。

    Args:
        line: ffmpeg 输出的一行文本。
    Returns:
        毫秒整数；该行不含 Duration 或值为 ``N/A`` 时返回 ``None``。
    Notes:
        ``-hide_banner`` 只屏蔽版本横幅，不影响输入流信息里的 Duration 行，
        所以这条兜底在现有命令行下依然有效。
    """
    m = _DURATION_RE.search(line or "")
    if not m:
        return None
    h, mi, s, frac = m.groups()
    ms = (int(h) * 3600 + int(mi) * 60 + int(s)) * 1000
    ms += int((frac + "000")[:3])
    return ms or None


def find_ffprobe(ffmpeg_path: str | None = None) -> str | None:
    """定位 ffprobe：优先取 ffmpeg 同目录的同胞，再退到 PATH。

    Args:
        ffmpeg_path: 已知的 ffmpeg 路径；``None`` 时只搜 PATH。
    Returns:
        绝对路径；找不到返回 ``None``。
    """
    if ffmpeg_path:
        parent = Path(ffmpeg_path).parent
        for name in ("ffprobe.exe", "ffprobe"):
            cand = parent / name
            if cand.is_file():
                return str(cand)
    return shutil.which("ffprobe")


def probe_duration_ms(src: str, ffmpeg_path: str | None = None) -> int | None:
    """用 ffprobe 预取媒体总时长，作为进度百分比的分母。

    Args:
        src: 媒体文件路径。
        ffmpeg_path: ffmpeg 路径，用于就近找 ffprobe。
    Returns:
        毫秒整数；ffprobe 缺失、超时、或对象无时长（如单张图片）时返回 ``None``。
    Notes:
        单次调用约 30ms，相对动辄几分钟的转码可以忽略。失败一律静默返回
        ``None``，由调用方回退到 ``Duration:`` 横幅或假进度条。
    """
    probe = find_ffprobe(ffmpeg_path)
    if not probe:
        return None
    try:
        proc = run_silent(
            [
                probe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                src,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        raw = (proc.stdout or "").strip()
        if not raw or raw.upper() == "N/A":
            return None
        ms = int(float(raw) * 1000)
        return ms if ms > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None  # 静默原因：探测失败不该阻断转码，交由上层回退


@dataclass
class ProgressSnapshot:
    """一次进度采样的全部可展示信息。

    Attributes:
        pct: 0..100 百分比；总时长未知时为 ``None``。
        out_time_ms: 已编码到的位置（毫秒）。
        duration_ms: 总时长（毫秒）；未知为 ``None``。
        speed: 编码速度倍率，如 ``1.53`` 表示 1.53x；未知为 ``None``。
        eta_sec: 预计剩余秒数；总时长或速度未知时为 ``None``。
        fps: 当前编码帧率；未知为 ``None``。
        total_size: 已写出字节数；未知为 ``None``。
        finished: 是否收到 ``progress=end``。
    """

    pct: int | None = None
    out_time_ms: int = 0
    duration_ms: int | None = None
    speed: float | None = None
    eta_sec: float | None = None
    fps: float | None = None
    total_size: int | None = None
    finished: bool = False


class FFmpegProgressParser:
    """逐行喂入 ffmpeg 输出，按「进度帧」吐出 :class:`ProgressSnapshot`。

    ffmpeg 的 ``-progress`` 是**成组输出**的：一组若干行 key=value，以
    ``progress=continue``（或 ``progress=end``）收尾。本类在收到收尾行时才
    吐一次快照，天然按组去重，不会一行一刷。

    典型用法::

        parser = FFmpegProgressParser(duration_ms=probe_duration_ms(src, exe))
        for line in proc.stdout:
            snap = parser.feed(line)
            if snap and snap.pct is not None:
                on_progress(snap.pct)

    Args:
        duration_ms: ffprobe 预取的总时长；``None`` 时会尝试从
            ``Duration:`` 横幅自动补齐。
        min_interval: 两次吐出快照的最小间隔（秒），用于给 UI 降压。
            ``progress=end`` 不受节流限制，一定会吐。
        clock: 取当前时刻的函数，注入以便测试。
    """

    def __init__(
        self,
        duration_ms: int | None = None,
        min_interval: float = 0.25,
        clock=None,
    ):
        import time

        self.duration_ms = duration_ms
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock or time.monotonic
        # 必须是负无穷而不是 0.0：注入的假时钟（以及某些平台上刚启动的
        # monotonic）起点就在 0 附近，用 0.0 当初值会让**第一组**进度被
        # 误判成「间隔不足」直接丢掉，进度条要等到第二组才动。
        self._last_emit = float("-inf")
        self._cur: dict[str, str] = {}
        self._last_pct = -1

    # ---- 内部：把当前累积的 key=value 组算成快照 ----
    def _snapshot(self, finished: bool) -> ProgressSnapshot:
        cur = self._cur
        out_ms = 0
        # 优先级：out_time（字符串，语义无歧义）> out_time_us（真微秒）
        # > out_time_ms（ffmpeg 历史 bug，实际也是微秒）
        if "out_time" in cur:
            out_ms = parse_hhmmss(cur["out_time"]) or 0
        if not out_ms and cur.get("out_time_us", "").isdigit():
            out_ms = int(cur["out_time_us"]) // 1000
        if not out_ms and cur.get("out_time_ms", "").isdigit():
            out_ms = int(cur["out_time_ms"]) // 1000

        speed = None
        sm = _SPEED_RE.match(cur.get("speed", "").strip().rstrip("x"))
        if sm:
            try:
                v = float(sm.group(1))
                speed = v if v > 0 else None
            except ValueError:
                speed = None

        fps = None
        try:
            v = float(cur.get("fps", "") or 0)
            fps = v if v > 0 else None
        except ValueError:
            pass

        total_size = None
        if cur.get("total_size", "").isdigit():
            total_size = int(cur["total_size"])

        pct = None
        eta = None
        if self.duration_ms and self.duration_ms > 0:
            pct = int(out_ms / self.duration_ms * 100)
            pct = max(0, min(100, pct))
            if speed:
                remain_ms = max(0, self.duration_ms - out_ms)
                eta = remain_ms / 1000.0 / speed
        if finished:
            pct = 100
            eta = 0.0

        return ProgressSnapshot(
            pct=pct,
            out_time_ms=out_ms,
            duration_ms=self.duration_ms,
            speed=speed,
            eta_sec=eta,
            fps=fps,
            total_size=total_size,
            finished=finished,
        )

    def feed(self, line: str) -> ProgressSnapshot | None:
        """喂一行 ffmpeg 输出。

        Args:
            line: 原始行（可含换行，内部会 strip）。
        Returns:
            该行凑齐了一组进度就返回快照，否则返回 ``None``。
            被节流丢弃的组也返回 ``None``。
        """
        line = (line or "").strip()
        if not line:
            return None

        # 非 key=value 行：只可能是横幅/日志，尝试补总时长
        if "=" not in line or line.startswith(" "):
            if self.duration_ms is None:
                got = parse_banner_duration(line)
                if got:
                    self.duration_ms = got
                    log.debug("从横幅补到总时长：%d ms", got)
            return None

        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()

        if key != "progress":
            self._cur[key] = val
            return None

        # 收到组尾，结算
        finished = val == "end"
        snap = self._snapshot(finished)
        self._cur.clear()

        if not finished:
            now = self._clock()
            if now - self._last_emit < self._min_interval:
                return None  # 节流：这一组丢掉，下一组马上又来
            # 百分比没变且没有速度信息可刷，就不打扰 UI
            if snap.pct is not None and snap.pct == self._last_pct and snap.speed is None:
                return None
            self._last_emit = now

        if snap.pct is not None:
            self._last_pct = snap.pct
        return snap


def format_eta(seconds: float | None) -> str:
    """把剩余秒数格式化成 ``MM:SS`` / ``H:MM:SS``。

    Args:
        seconds: 剩余秒数；``None`` 或负数返回 ``"--:--"``。
    Returns:
        供 UI 直接显示的字符串。
    """
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_speed(speed: float | None) -> str:
    """把编码速度倍率格式化成 ``1.53x``。

    Args:
        speed: 速度倍率；``None`` 返回 ``"--"``。
    Returns:
        供 UI 直接显示的字符串。
    """
    if not speed or speed <= 0:
        return "--"
    if speed >= 10:
        return f"{speed:.0f}x"
    return f"{speed:.2f}x"
