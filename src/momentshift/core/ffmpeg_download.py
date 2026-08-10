"""下载 ffmpeg + ffprobe 静态二进制到目标目录。

职责边界：
- 做：纯标准库网络请求 + 解包，提供应用内「一键下载」与 CLI 封装共用的唯一实现。
  V0.9.1 重构：多镜像源自动回退、流式下载带进度、断线重试用、下载后校验、
  以及面向用户的友好错误提示 —— 让一键部署在弱网 / 被墙环境下也能成功。
- 不做：不调用 ffmpeg；不管理下载进度 UI（由 FfmpegDownloadWorker 包装成信号）。

依赖：标准库；被依赖：gui/ffmpeg_card、tools/download_ffmpeg。
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urlparse

from .qt_compat import QObject, QRunnable, Signal


# 每个镜像源的最大尝试次数（应对瞬时网络抖动）。
_MAX_ATTEMPTS = 2
# 单步网络读写的超时（秒）。连接长时间无数据即判定卡死并重试 / 换源。
_SOCKET_TIMEOUT = 30
# 流式下载分块大小。
_CHUNK = 256 * 1024


class DownloadSignals(QObject):
    """后台下载 worker 向 GUI 线程回传状态的信号载体。

    线程约定：信号在 worker 线程发出，由 Qt 队列连接切回 GUI 线程。
    信号：
    - ``started()`` —— 下载开始。
    - ``progress(int, int)`` —— ``(已下载字节, 总字节)``；总字节未知时为 0。
    - ``finished(bool, str)`` —— 下载结束，参数为 ``(是否成功, 失败原因或空串)``。
    """

    started = Signal()
    progress = Signal(int, int)
    finished = Signal(bool, str)


def _platform_sources():
    """返回当前平台的 ffmpeg 静态构建下载源（按优先级排序）。

    Returns:
        ``[(下载地址, 包类型)]`` 列表，包类型为 ``zip`` 或 ``txz``。
        排在前面的源优先尝试；任一个失败会自动回退到下一个。
    """
    if sys.platform.startswith("win"):
        # 顺序针对「多数用户反馈无法下载」专门调过：
        # 1) ghproxy.net 镜像的 GitHub 构建 —— 国内可达性最好（已验证 HTTP 200），
        #    且 BtbN 的 latest 发布永远是最新版，无需维护版本号。
        # 2) gyan.dev 官方 essentials 构建 —— 体积小（仅 ffmpeg+ffprobe），
        #    但国内常被墙，作为国际网络下的首选补充。
        # 3) 直连 GitHub 的 BtbN latest —— 兜底（无代理时海外可用）。
        return [
            (
                "https://ghproxy.net/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
                "zip",
            ),
            ("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip", "zip"),
            (
                "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
                "zip",
            ),
        ]
    if sys.platform == "darwin":
        return [
            ("https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip", "zip"),
            ("https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip", "zip"),
        ]
    machine = "amd64"  # 取不到架构时的保守默认值
    try:
        machine = os.uname().machine
    except AttributeError:  # 静默原因：Windows 没有 os.uname，直接按 amd64 处理
        pass
    arch = "arm64" if machine in ("aarch64", "arm64") else "amd64"
    return [
        (
            f"https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{arch}-static.tar.xz",
            "txz",
        )
    ]


def _is_wanted(name, is_windows):
    """判断压缩包内的某个成员是否是我们要提取的 ffmpeg/ffprobe 可执行文件。

    Windows 下额外提取 ffplay（让「运行环境」检测更完整），但缺失 ffplay
    不算失败，因此校验阶段只要求 ffmpeg / ffprobe。
    """
    base = os.path.basename(name)
    if is_windows:
        return base in ("ffmpeg.exe", "ffprobe.exe", "ffplay.exe")
    return os.path.basename(base) in ("ffmpeg", "ffprobe")


def _host_label(url: str) -> str:
    """把下载地址映射成一个对用户友好的来源名（用于错误提示）。"""
    host = urlparse(url).netloc
    if "ghproxy" in host:
        return "ghproxy 镜像"
    if "gyan.dev" in host:
        return "gyan.dev"
    if "github.com" in host:
        return "GitHub"
    if "evermeet.cx" in host:
        return "evermeet.cx"
    if "johnvansickle.com" in host:
        return "johnvansickle.com"
    return host or url


def _friendly_error(exc: BaseException) -> str:
    """把异常翻译成面向用户的中文提示，区分网络 / 服务器 / 文件损坏等情形。"""
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        if code in (403, 404):
            return f"下载地址无效（HTTP {code}）"
        if 500 <= code < 600:
            return f"下载服务器出错（HTTP {code}）"
        return f"下载失败（HTTP {code}）"
    if isinstance(exc, urllib.error.URLError):
        text = str(exc.reason).lower()
        if "timed out" in text or "timeout" in text:
            return "连接超时，请检查网络后重试"
        if "name or service not known" in text or "getaddrinfo" in text or "errno 11001" in text or "errno -2" in text:
            return "无法解析服务器地址（DNS / 网络异常）"
        if "connection refused" in text or "connection reset" in text or "远程主机" in text:
            return "连接被拒绝或中断"
        if "ssl" in text or "certificate" in text:
            return "SSL / 证书校验失败"
        return f"网络错误：{exc.reason}"
    if isinstance(exc, (zipfile.BadZipFile, tarfile.TarError)):
        return "下载的文件已损坏，正在换源重试"
    if isinstance(exc, socket.timeout):
        return "连接超时，请检查网络后重试"
    if isinstance(exc, RuntimeError):
        # 校验阶段的自定义消息，原样透出。
        return str(exc)
    return str(exc)


def _stream_download(url, dest_dir, on_progress, timeout):
    """流式下载到临时文件，返回 ``(临时文件路径, 已下载字节, 总字节)``。

    下载全程分块写入磁盘并回调进度；任何异常都会清理临时文件后上抛，
    交由上层决定重试或换源。总字节未知（chunked 传输）时以 0 表示。
    """
    req = urllib.request.Request(url, headers={"User-Agent": "MomentShift"})
    fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".part")
    downloaded = 0
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                total = int(resp.headers.get("Content-Length", 0) or 0)
                if on_progress:
                    on_progress(0, total)
                while True:
                    buf = resp.read(_CHUNK)
                    if not buf:
                        break
                    out.write(buf)
                    downloaded += len(buf)
                    if on_progress:
                        on_progress(downloaded, total)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    return tmp_path, downloaded, total


def _extract_archive(tmp_path, dest_dir, kind):
    """把临时压缩包里的 ffmpeg/ffprobe（及 Windows 下的 ffplay）解压到目标目录。"""
    is_windows = sys.platform.startswith("win")
    if kind == "zip":
        with zipfile.ZipFile(tmp_path) as zf:
            for member in zf.namelist():
                if _is_wanted(member, is_windows):
                    out_name = os.path.basename(member)
                    with (
                        zf.open(member) as src,
                        open(os.path.join(dest_dir, out_name), "wb") as out,
                    ):
                        shutil.copyfileobj(src, out)
    else:  # tar.xz
        with tarfile.open(tmp_path, mode="r:*") as tf:
            for member in tf.getmembers():
                if _is_wanted(member.name, is_windows):
                    base = os.path.basename(member.name)
                    f = tf.extractfile(member)
                    if f is None:
                        continue
                    with open(os.path.join(dest_dir, base), "wb") as out:
                        shutil.copyfileobj(f, out)


def _verify_binaries(dest_dir):
    """校验 ffmpeg/ffprobe 已就位且非空，返回缺失项列表（空 = 成功）。"""
    is_windows = sys.platform.startswith("win")
    names = ("ffmpeg.exe", "ffprobe.exe") if is_windows else ("ffmpeg", "ffprobe")
    missing = []
    for name in names:
        path = os.path.join(dest_dir, name)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            missing.append(name)
    return missing


def download_ffmpeg(dest_dir, on_progress=None):
    """下载 ffmpeg 与 ffprobe 到指定目录（多源容错）。

    Args:
        dest_dir: 目标目录，不存在时自动创建。
        on_progress: 可选回调 ``(downloaded:int, total:int)``，用于上报进度；
            ``total`` 为 0 表示服务器未给出总大小（无法显示百分比）。
    Returns:
        ``(是否成功, 失败原因或空串)``。失败时原因已含全部源的错误明细与
        可操作建议，可直接展示给用户。
    """
    os.makedirs(dest_dir, exist_ok=True)
    # 清掉上次可能残留的半截下载，避免占空间或干扰校验。
    for name in os.listdir(dest_dir):
        if name.endswith(".part"):
            try:
                os.remove(os.path.join(dest_dir, name))
            except OSError:
                pass

    is_windows = sys.platform.startswith("win")
    errors = []
    for url, kind in _platform_sources():
        last_exc = None
        for _ in range(_MAX_ATTEMPTS):
            tmp_path = None
            try:
                tmp_path, _, _ = _stream_download(url, dest_dir, on_progress, _SOCKET_TIMEOUT)
                _extract_archive(tmp_path, dest_dir, kind)
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                missing = _verify_binaries(dest_dir)
                if missing:
                    raise RuntimeError("下载完成但未找到 " + "、".join(missing))
                # 类 Unix 系统上从压缩包解出的文件默认没有执行位，必须补上。
                if not is_windows:
                    for name in ("ffmpeg", "ffprobe"):
                        p = os.path.join(dest_dir, name)
                        if os.path.exists(p):
                            os.chmod(p, 0o755)
                return True, ""
            except BaseException as exc:  # noqa: BLE001 - 需捕获所有异常以换源
                last_exc = exc
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                continue
        errors.append(
            f"{_host_label(url)}（{_MAX_ATTEMPTS} 次尝试均失败：{_friendly_error(last_exc)}）"
        )
    detail = "；".join(errors)
    hint = "所有镜像源均不可用，请检查网络后重试，或手动从 ffmpeg.org 下载并放到软件目录。"
    return False, f"{detail}。{hint}"


class FfmpegDownloadWorker(QRunnable):
    """在线程池里执行 :func:`download_ffmpeg`，避免阻塞界面。

    典型用法::

        worker = FfmpegDownloadWorker(str(ffmpeg_install_dir()))
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_done)
        QThreadPool.globalInstance().start(worker)

    线程约定：run() 在工作线程执行，只允许通过 signals 回主线程；
    signals 必须由调用方持有引用，否则会随局部变量被回收。
    """

    def __init__(self, dest_dir: str):
        super().__init__()
        self.setAutoDelete(True)
        self.dest_dir = dest_dir
        self.signals = DownloadSignals()

    def run(self) -> None:
        """下载并解压 ffmpeg，全过程通过信号上报。"""
        self.signals.started.emit()

        def _cb(downloaded: int, total: int) -> None:
            self.signals.progress.emit(downloaded, total)

        ok, msg = download_ffmpeg(self.dest_dir, on_progress=_cb)
        self.signals.finished.emit(ok, msg)
