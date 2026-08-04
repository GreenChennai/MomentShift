"""FunASR 模型下载（多源 + curl 优先后端）。

职责边界：
- 做：按模型清单把文件下载到 ``tools/funasr/<model-id>/``，带进度回调与
  校验（Content-Length / 落盘大小），并提供 Qt worker。
- 不做：不加载模型（``core/funasr_engine``）；不弹界面（``gui/asr_interface``）。

模型源：
- HuggingFace 直链（``User-Agent: MomentShift``）——每个文件可配多个备选 URL。
- ModelScope（魔搭，国内直连）——paraformer-large 等官方 onnx 镜像。
- GitHub：FunASR 模型文件普遍超过 GitHub 单文件 100MB 限制，无可用镜像；
  选择 GitHub 源时自动回退 ModelScope → HuggingFace。

v0.8.8 下载后端（用户实测 urllib 在构建 exe 里仍报 unknown url type）：
- **curl.exe 优先**：Windows 10 1803+ 自带，走 Schannel 原生 TLS，与浏览器同
  栈；显式读取系统代理（urllib.getproxies 读注册表/环境变量）传给 ``--proxy``，
  代理优先、直连兜底——用户浏览器能下载 HF 的场景 curl 必有一路可达。
- **urllib 双通道回退**：curl 不存在（Win7 等）时走直连 → 系统代理。
- 进度：curl 写 ``.part`` 期间轮询文件大小；取消 = ``terminate()``。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .funasr_engine import MODEL_CATALOG, model_dir
from .logger import get_logger
from .platform import WIN_SILENT
from .qt_compat import QObject, QRunnable, Signal

log = get_logger("funasr_download")

_USER_AGENT = "MomentShift"
_TIMEOUT = 300.0
_CHUNK = 256 * 1024
_CURL = r"C:\Windows\System32\curl.exe"  # Windows 10 1803+ 自带
_SIZE_TOLERANCE = 0.01  # 大小校验宽容差（HF resolve/main 是软链，文件可能更新）


class _DownloadCancelled(Exception):
    """用户取消下载。"""


def find_spec(model_id: str) -> dict | None:
    """按 id 查模型清单；未知 id 返回 None。"""
    for spec in MODEL_CATALOG:
        if spec["id"] == model_id:
            return spec
    return None


# =============================================================================
# 下载后端
# =============================================================================
def _curl_available() -> bool:
    """curl.exe 是否可用（优先系统自带路径，其次 PATH）。"""
    return bool(os.path.isfile(_CURL) or shutil.which("curl"))


def _curl_exe() -> str:
    if os.path.isfile(_CURL):
        return _CURL
    return shutil.which("curl") or "curl"


def _system_proxy_url() -> str | None:
    """读系统代理 URL（urllib 在 Windows 会读注册表 + 环境变量）。"""
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 - 读代理失败当没有
        proxies = {}
    for key in ("https", "https_proxy", "HTTPS_PROXY"):
        val = proxies.get(key)
        if val:
            return val
    for key in ("http", "http_proxy", "HTTP_PROXY"):
        val = proxies.get(key)
        if val:
            return val
    return None


def _curl_download(
    url: str,
    tmp: str,
    expected_size: int | None,
    progress_cb,
    cancel: threading.Event | None,
) -> int:
    """用 curl.exe 下载到 ``tmp``（.part），返回实际字节数。

    代理优先（用户浏览器能下载的场景）、直连兜底；两路都失败抛 RuntimeError。
    """
    proxy = _system_proxy_url()
    attempts: list[list[str]] = []
    if proxy:
        attempts.append(["--proxy", proxy])
    attempts.append(["--noproxy", "*"])
    last_err = ""

    for extra in attempts:
        if cancel is not None and cancel.is_set():
            raise _DownloadCancelled()
        cmd = [
            _curl_exe(),
            "-L",           # 跟随 307 重定向（HF resolve/main → CDN）
            "--fail",       # 非 2xx 视为失败（不落 HTML 错误页）
            "-sS",          # 静默，仅错误输出到 stderr
            "--connect-timeout", "20",
            "-o", tmp,
            url,
            *extra,
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=WIN_SILENT,
        )
        last_size = 0
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                raise _DownloadCancelled()
            if progress_cb is not None:
                try:
                    done = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                    last_size = done
                    progress_cb(done, expected_size or done)
                except OSError:
                    pass
            time.sleep(0.15)
        if proc.returncode == 0:
            # 小文件可能在首次轮询前就下完，用最终落盘大小（而非轮询快照）
            try:
                done = os.path.getsize(tmp)
            except OSError:
                done = last_size
            if progress_cb is not None:
                progress_cb(done, expected_size or done)
            return done
        try:
            err = proc.stderr.read().decode("utf-8", "replace").strip()[:200]
        except OSError:
            err = ""
        last_err = f"curl {proc.returncode}：{err or '网络错误'}"
        log.warning("curl 下载 %s 失败（%s）", url, last_err)
        try:
            os.remove(tmp)
        except OSError:
            pass
    raise RuntimeError(last_err)


def _urllib_download(
    url: str,
    tmp: str,
    expected_size: int | None,
    progress_cb,
    cancel: threading.Event | None,
) -> int:
    """urllib 双通道（直连 → 系统代理）下载到 ``tmp``，返回实际字节数。"""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    resp = None
    try:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            resp = opener.open(req, timeout=_TIMEOUT)
        except Exception:  # noqa: BLE001 - 直连失败回退系统代理
            opener2 = urllib.request.build_opener()
            resp = opener2.open(req, timeout=_TIMEOUT)
        total = int(resp.headers.get("Content-Length") or 0) or expected_size or 0
        done = 0
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
                    progress_cb(done, total or done)
        return done
    finally:
        if resp is not None:
            resp.close()


def _source_urls(f: dict, source: str) -> list[str]:
    """按所选源展开文件的候选 URL 列表（含自动回退链）。

    - hf：HuggingFace 直链（清单 ``urls``）。
    - github（默认）：GitHub 无模型镜像（FunASR onnx 普遍超 GitHub 单文件
      100MB 限制）→ 自动回退 HuggingFace。
    """
    hf = list(f.get("urls") or [])
    gh = list(f.get("gh_urls") or [])
    if source == "hf":
        return hf
    return gh + hf


def _download_file(
    url: str,
    dest_path: str,
    expected_size: int | None = None,
    progress_cb=None,
    cancel: threading.Event | None = None,
) -> None:
    """下载单个文件到 ``dest_path``（先写 ``.part`` 再原子改名）。"""
    tmp = dest_path + ".part"
    parent = os.path.dirname(dest_path)
    os.makedirs(parent or ".", exist_ok=True)
    try:
        if _curl_available():
            done = _curl_download(url, tmp, expected_size, progress_cb, cancel)
        else:
            done = _urllib_download(url, tmp, expected_size, progress_cb, cancel)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if expected_size and abs(done - expected_size) > max(256, expected_size * _SIZE_TOLERANCE):
        # HF resolve/main 是软链，文件可能更新，清单 size 只是参考；宽容忍差且不删文件。
        log.warning(
            "文件 %s 大小与清单不符（期望 %s，实际 %s），按实际为准继续",
            dest_path,
            expected_size,
            done,
        )
    os.replace(tmp, dest_path)


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def download_model(
    model_id: str,
    dest_dir: str | None = None,
    progress_cb=None,
    cancel: threading.Event | None = None,
    source: str = "github",
) -> tuple[bool, str]:
    """下载一个模型到目标目录（默认 ``tools/funasr/<model-id>/``）。

    Args:
        model_id: 模型清单里的 id。
        dest_dir: 覆盖默认目录（测试用）。
        progress_cb: ``cb(pct: int)``，0..100 整体进度。
        cancel: 置位后中止。
        source: 首选源 ``hf``（HuggingFace）或 ``github``（默认；GitHub 无模型
            镜像时自动回退 HuggingFace）。

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

        for url in _source_urls(f, source):
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
                if "unknown url type" in err_text:
                    err_text = (
                        "网络代理配置异常或无法访问模型源，请检查系统代理/网络设置，"
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

    def __init__(
        self,
        model_id: str,
        dest_dir: str | None = None,
        cancel=None,
        source: str = "github",
    ):
        super().__init__()
        self.setAutoDelete(True)
        self.model_id = model_id
        self.dest_dir = dest_dir
        self.cancel = cancel
        self.source = source
        self.signals = FunasrDownloadSignals()

    def run(self) -> None:
        self.signals.started.emit(self.model_id)
        ok, msg = download_model(
            self.model_id,
            self.dest_dir,
            progress_cb=lambda pct: self.signals.progress.emit(self.model_id, pct),
            cancel=self.cancel,
            source=self.source,
        )
        self.signals.finished.emit(self.model_id, ok, msg)
