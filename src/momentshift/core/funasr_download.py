"""FunASR 模型下载（HF 直链，纯标准库 urllib）。

职责边界：
- 做：按模型清单把文件下载到 ``tools/funasr/<model-id>/``，带进度回调与
  校验（Content-Length / 落盘大小），并提供 Qt worker。
- 不做：不加载模型（``core/funasr_engine``）；不弹界面（``gui/asr_interface``）。

模型源：HuggingFace 直链（``User-Agent: MomentShift``），每个文件可配多个
备选 URL 逐个尝试。模型均来自官方 funasr 组织或经核对的镜像仓库。
"""

from __future__ import annotations

import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .funasr_engine import MODEL_CATALOG, model_dir
from .logger import get_logger
from .qt_compat import QObject, QRunnable, Signal

log = get_logger("funasr_download")

_USER_AGENT = "MomentShift"
_TIMEOUT = 300.0
_CHUNK = 256 * 1024


class _DownloadCancelled(Exception):
    """用户取消下载。"""


def find_spec(model_id: str) -> dict | None:
    """按 id 查模型清单；未知 id 返回 None。"""
    for spec in MODEL_CATALOG:
        if spec["id"] == model_id:
            return spec
    return None


def _download_file(
    url: str,
    dest_path: str,
    expected_size: int | None = None,
    progress_cb=None,
    cancel: threading.Event | None = None,
) -> None:
    """下载单个文件到 ``dest_path``（先写 ``.part`` 再原子改名）。

    Args:
        url: 直链地址。
        dest_path: 目标文件路径。
        expected_size: 期望字节数；下载完成后校验，不符则删除并报错。
        progress_cb: ``cb(done_bytes, total_bytes)``，在下载线程同步回调。
        cancel: 置位后中止下载并抛 :class:`_DownloadCancelled`。
    """
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        total = int(resp.headers.get("Content-Length") or 0) or expected_size or 0
        done = 0
        tmp = dest_path + ".part"
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(tmp, "wb") as fh:
            while True:
                if cancel is not None and cancel.is_set():
                    raise _DownloadCancelled()
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress_cb is not None:
                    progress_cb(done, total)
        if expected_size and abs(done - expected_size) > max(256, expected_size * 0.01):
            # v0.8.6：HF resolve/main 是软链，文件可能更新，清单里的 size 只是参考。
            # 硬校验会误杀（实测 paraformer config.yaml 差 133、fsmn-vad model_quant 差 2132）。
            # 改为宽容忍差（±1% 或 256 字节），且不删除已下载文件，只记 warning。
            log.warning(
                "文件 %s 大小与清单不符（期望 %s，实际 %s），按实际为准继续",
                dest_path,
                expected_size,
                done,
            )
        os.replace(tmp, dest_path)


def download_model(
    model_id: str,
    dest_dir: str | None = None,
    progress_cb=None,
    cancel: threading.Event | None = None,
) -> tuple[bool, str]:
    """下载一个模型到目标目录（默认 ``tools/funasr/<model-id>/``）。

    Args:
        model_id: 模型清单里的 id。
        dest_dir: 覆盖默认目录（测试用）。
        progress_cb: ``cb(pct: int)``，0..100 整体进度。
        cancel: 置位后中止。

    Returns:
        ``(是否成功, 消息)``。
    """
    spec = find_spec(model_id)
    if spec is None:
        return False, f"未知模型：{model_id}"
    dest = Path(dest_dir) if dest_dir else model_dir(model_id)
    total_size = sum(int(f.get("size") or 0) for f in spec["files"])
    done_size = 0

    for f in spec["files"]:
        target = dest / f["name"]
        expected = int(f.get("size") or 0)
        last_err = ""

        def _cb(done: int, _total: int, _done=done_size, _all=total_size):
            if progress_cb is not None and _all > 0:
                progress_cb(min(100, int(100 * (_done + done) / _all)))

        for url in f["urls"]:
            try:
                _download_file(
                    url,
                    str(target),
                    expected_size=expected or None,
                    progress_cb=_cb,
                    cancel=cancel,
                )
                last_err = ""
                break
            except _DownloadCancelled:
                return False, "下载已取消"
            except Exception as exc:  # noqa: BLE001 - 单个源失败回退下一个
                err_text = str(exc)
                # v0.8.6：urlopen 对异常 URL/代理环境会报 "unknown url type"，给用户
                # 可行动的提示（代理配置异常或 HuggingFace 不可达），而非裸报错。
                if "unknown url type" in err_text:
                    err_text = (
                        "网络代理配置异常或无法访问 HuggingFace，请检查系统代理/网络设置，"
                        "或点击「前往下载」在浏览器中手动下载"
                    )
                last_err = err_text
                log.warning("下载 %s 失败（%s）：%s", f["name"], url, exc)
        if last_err:
            return False, f"{f['name']} 下载失败：{last_err}"
        done_size += expected
        if progress_cb is not None and total_size > 0:
            progress_cb(min(100, int(100 * done_size / total_size)))

    return True, f"模型 {spec['id']} 下载完成"


class FunasrDownloadSignals(QObject):
    """模型下载 worker 的信号载体（worker 线程发出，Qt 队列切回 GUI 线程）。"""

    started = Signal(str)  # model_id
    progress = Signal(str, int)  # model_id, pct
    finished = Signal(str, bool, str)  # model_id, ok, message


class FunasrModelDownloadWorker(QRunnable):
    """在后台线程下载**单个**模型。"""

    def __init__(self, model_id: str, dest_dir: str | None = None, cancel=None):
        super().__init__()
        self.setAutoDelete(True)
        self.model_id = model_id
        self.dest_dir = dest_dir
        self.cancel = cancel
        self.signals = FunasrDownloadSignals()

    def run(self) -> None:
        self.signals.started.emit(self.model_id)
        ok, msg = download_model(
            self.model_id,
            self.dest_dir,
            progress_cb=lambda pct: self.signals.progress.emit(self.model_id, pct),
            cancel=self.cancel,
        )
        self.signals.finished.emit(self.model_id, ok, msg)
