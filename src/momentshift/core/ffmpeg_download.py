"""下载 ffmpeg + ffprobe 静态二进制到目标目录。

职责边界：
- 做：纯标准库网络请求 + 解包，提供应用内「一键下载」与 CLI 封装共用的唯一实现。
- 不做：不调用 ffmpeg；不管理下载进度 UI（由 FfmpegDownloadWorker 包装）。

依赖：标准库；被依赖：gui/ffmpeg_card、tools/download_ffmpeg。
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tarfile
import urllib.request
import zipfile

from .qt_compat import QObject, QRunnable, Signal


class DownloadSignals(QObject):
    """后台下载 worker 向 GUI 线程回传状态的信号载体。

    线程约定：信号在 worker 线程发出，由 Qt 队列连接切回 GUI 线程。
    信号：
    - ``started()`` —— 下载开始。
    - ``finished(bool, str)`` —— 下载结束，参数为 ``(是否成功, 失败原因或空串)``。
    """

    started = Signal()
    finished = Signal(bool, str)


def _platform_sources():
    """返回当前平台的 ffmpeg 静态构建下载源。

    Returns:
        ``[(下载地址, 包类型)]`` 列表，包类型为 ``zip`` 或 ``txz``。
        macOS 需要分别下载 ffmpeg 与 ffprobe，故返回两条。
    """
    if sys.platform.startswith("win"):
        return [("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip", "zip")]
    if sys.platform == "darwin":
        return [
            ("https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip", "zip"),
            ("https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip", "zip"),
        ]
    # Linux
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
    """判断压缩包内的某个成员是否是我们要提取的 ffmpeg/ffprobe 可执行文件。"""
    base = os.path.basename(name)
    if is_windows:
        return base in ("ffmpeg.exe", "ffprobe.exe")
    return os.path.basename(base) in ("ffmpeg", "ffprobe")


def _download(url: str, dest_dir: str) -> None:
    """下载单个包并把其中的 ffmpeg/ffprobe 解压到目标目录。

    Args:
        url: 下载地址，按扩展名区分 zip 与 tar.xz 两种解包方式。
        dest_dir: 解压目标目录，须已存在。
    Notes:
        这里的 print 是刻意保留的：本模块同时被命令行脚本
        ``tools/download_ffmpeg`` 复用，需要在终端看到实时进度。
    """
    print(f"[ffmpeg_download] {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "MomentShift"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()

    is_windows = sys.platform.startswith("win")
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if _is_wanted(member, is_windows):
                    out_name = os.path.basename(member)
                    with (
                        zf.open(member) as src,
                        open(os.path.join(dest_dir, out_name), "wb") as out,
                    ):
                        shutil.copyfileobj(src, out)
                    print(f"[ffmpeg_download] extracted {out_name}")
    else:  # tar.xz
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tf:
            for member in tf.getmembers():
                if _is_wanted(member.name, is_windows):
                    base = os.path.basename(member.name)
                    f = tf.extractfile(member)
                    with open(os.path.join(dest_dir, base), "wb") as out:
                        shutil.copyfileobj(f, out)
                    print(f"[ffmpeg_download] extracted {base}")


def download_ffmpeg(dest_dir: str) -> tuple[bool, str]:
    """下载 ffmpeg 与 ffprobe 到指定目录。

    Args:
        dest_dir: 目标目录，不存在时自动创建。
    Returns:
        ``(是否成功, 失败原因或空串)``
    """
    os.makedirs(dest_dir, exist_ok=True)
    is_windows = sys.platform.startswith("win")
    try:
        for url, _kind in _platform_sources():
            _download(url, dest_dir)
        # 类 Unix 系统上从压缩包解出来的文件默认没有执行位，必须补上
        if not is_windows:
            for name in ("ffmpeg", "ffprobe"):
                path = os.path.join(dest_dir, name)
                if os.path.exists(path):
                    os.chmod(path, 0o755)
        return True, ""
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
        # 网络中断、磁盘写失败、包损坏都归一为「失败 + 原因」交给界面提示
        return False, str(exc)


class FfmpegDownloadWorker(QRunnable):
    """在线程池里执行 :func:`download_ffmpeg`，避免阻塞界面。

    典型用法::

        worker = FfmpegDownloadWorker(str(ffmpeg_install_dir()))
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
        ok, msg = download_ffmpeg(self.dest_dir)
        self.signals.finished.emit(ok, msg)
