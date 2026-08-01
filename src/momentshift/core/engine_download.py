"""下载「引擎 + 模型」到 ``tools/<eid>/``（v0.7.6 功能 2）。

为每个引擎提供按优先级排序的下载源：
  HuggingFace 发布文件 → GitHub 最新 Release 资源 → 官方直链。
纯标准库联网 + Qt worker，无第三方依赖（与 tools_download / ffmpeg_download 一致）。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import urllib.request
import zipfile

from .qt_compat import QObject, Signal, QRunnable


class EngineDownloadSignals(QObject):
    started = Signal(str)                 # 正在下载的引擎 eid
    finished = Signal(str, bool, str)     # (eid, ok, message)


# --------------------------------------------------------------------------
# 源解析
# --------------------------------------------------------------------------
def _github_latest_asset_url(repo: str, asset_substr: str) -> str | None:
    """解析 GitHub 最新 Release 中名字含 ``asset_substr`` 的资源下载地址。

    无法访问（限流 / 离线）时返回 ``None``，调用方回退到下一个源。
    """
    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "MomentShift"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for a in data.get("assets", []):
            name = (a.get("name") or "").lower()
            if asset_substr.lower() in name:
                return a.get("browser_download_url")
    except Exception as exc:  # 网络 / 限流 / 解析失败：交给下一个源
        print(f"[engine_download] github resolve failed for {repo}: {exc}")
    return None


def _resolve_source(kind: str, value: str) -> str | None:
    """把一条 (kind, value) 源解析成具体下载 URL。"""
    if kind == "gh":
        repo, _, substr = value.partition("|")
        return _github_latest_asset_url(repo, substr or "")
    if kind in ("hf", "url"):
        return value
    return None


# --------------------------------------------------------------------------
# 下载 + 解包
# --------------------------------------------------------------------------
def _extract_all(data: bytes, dest_dir: str) -> None:
    """把 zip 内的全部成员解压到 ``dest_dir``（保留目录结构）。

    引擎通常以「压缩包根目录含一个 <engine>-windows/ 文件夹」的形式发布，
    find_engine 的二级递归目录搜索会自动定位其中的可执行文件，故这里整包解压即可。
    """
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.namelist():
            target = os.path.join(dest_dir, member)
            if member.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            print(f"[engine_download] extracted {member}")


def download_engine(eid: str, dest_dir: str, sources: list) -> tuple[bool, str]:
    """按优先级逐个尝试 ``sources``，解压第一个下载成功的源。

    ``sources`` 是 ``[(kind, value), ...]``（优先级从高到低）。
    返回 ``(ok, message)``。
    """
    os.makedirs(dest_dir, exist_ok=True)
    last_err = ""
    for kind, value in sources:
        url = _resolve_source(kind, value)
        if not url:
            last_err = f"no URL for {kind}:{value}"
            continue
        try:
            print(f"[engine_download] {eid} <- {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "MomentShift"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = resp.read()
            _extract_all(data, dest_dir)
            return True, f"downloaded from {url}"
        except Exception as exc:
            last_err = str(exc)
            print(f"[engine_download] {eid} source failed: {exc}")
    return False, last_err or "no download source"


class EngineDownloadWorker(QRunnable):
    """在后台线程运行 :func:`download_engine`。"""

    def __init__(self, eid: str, dest_dir: str, sources: list):
        super().__init__()
        self.setAutoDelete(True)
        self.eid = eid
        self.dest_dir = dest_dir
        self.sources = sources
        self.signals = EngineDownloadSignals()

    def run(self) -> None:
        self.signals.started.emit(self.eid)
        ok, msg = download_engine(self.eid, self.dest_dir, self.sources)
        self.signals.finished.emit(self.eid, ok, msg)
