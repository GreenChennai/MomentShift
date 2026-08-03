"""可选图片压缩工具（oxipng / jpegoptim / gifsicle）的一键下载。

职责边界：
- 做：解析 GitHub 最新 release 地址、下载 zip、把需要的二进制解压到可执行文件
  旁的统一 ``tools/`` 目录；提供对应的 Qt worker。
- 不做：不执行压缩（见 core/compressor）；不判断工具是否已安装。

依赖：core/logger、core/qt_compat；被依赖：gui/engine_card、gui/setting_interface。

设计说明：
- 这几个二进制体积都很小（不像 ffmpeg），适合由应用自己一键下载并集中管理，
  免去让用户手动往 exe 旁边丢文件。
- Pillow 始终作为兜底后端，因此某个工具缺失只会让对应的高级后端不可用，
  不影响压缩功能整体可用。
- 只用标准库做网络请求 + Qt worker 封装，不引入额外 pip 依赖。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import urllib.request
import zipfile

from .logger import get_logger
from .qt_compat import QObject, QRunnable, Signal

log = get_logger("tools_download")


class DownloadSignals(QObject):
    """单个工具下载 worker 的信号载体。

    线程约定：信号在 worker 线程发出，由 Qt 队列连接切回 GUI 线程。
    信号：
    - ``started(str)`` —— 开始下载，参数为工具 id。
    - ``finished(str, bool, str)`` —— 下载结束，参数为 ``(工具 id, 是否成功, 消息)``。
    """

    started = Signal(str)
    finished = Signal(str, bool, str)


# --- 下载源解析 ---
# 每个条目描述如何获取该工具的 Windows 构建：优先取 GitHub「最新 release」的
# 附件，解析失败再退回写死的固定地址，保证限流时仍能装上。
_TOOLS: dict[str, dict] = {
    "oxipng": {
        "repo": "oxipng/oxipng",
        "asset": "x86_64-pc-windows-msvc.zip",
        "binaries": ["oxipng.exe"],
        "fallback": "https://github.com/oxipng/oxipng/releases/download/v10.1.1/oxipng-10.1.1-x86_64-pc-windows-msvc.zip",
    },
    "jpegoptim": {
        "repo": "tjko/jpegoptim",
        "asset": "x64-windows.zip",
        "binaries": ["jpegoptim.exe"],
        "fallback": "https://github.com/tjko/jpegoptim/releases/download/v1.5.6/jpegoptim-1.5.6-x64-windows.zip",
    },
    # gifsicle 专门用于 GIF 动图压缩：Pillow 压 GIF 会把多帧动画压成单帧
    "gifsicle": {
        "repo": "kohler/gifsicle",
        "asset": "win64",
        "binaries": ["gifsicle.exe"],
        "fallback": "https://github.com/kohler/gifsicle/releases/download/v1.95/gifsicle-1.95-win64.zip",
    },
}


def _github_latest_asset_url(repo: str, asset_substr: str) -> str | None:
    """查询 GitHub 最新 release 中名字含 ``asset_substr`` 的附件下载地址。

    Args:
        repo: ``owner/name`` 形式的仓库标识。
        asset_substr: 附件名需包含的子串，大小写不敏感。
    Returns:
        命中的下载地址；网络失败、限流或无匹配时返回 ``None``，由调用方回退到
        固定地址。
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
    except (OSError, ValueError, KeyError) as exc:
        # 网络不通 / 限流 / 返回体非预期 JSON，都只降级到固定回退地址
        log.warning("解析 %s 的最新 release 失败，回退固定地址：%s", repo, exc)
    return None


def _download_url(url: str, dest_dir: str, wanted: list[str]) -> list[str]:
    """下载 zip 并把其中指定的二进制解压到目标目录。

    Args:
        url: zip 包地址。
        dest_dir: 解压目标目录。
        wanted: 需要提取的文件名列表，匹配时忽略大小写与压缩包内层级。
    Returns:
        实际提取出的文件名列表；一个都没匹配到时为空列表。
    """
    req = urllib.request.Request(url, headers={"User-Agent": "MomentShift"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()
    extracted: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.namelist():
            base = os.path.basename(member)
            if base.lower() in (w.lower() for w in wanted):
                with zf.open(member) as src, open(os.path.join(dest_dir, base), "wb") as out:
                    shutil.copyfileobj(src, out)
                extracted.append(base)
                log.info("已解压 %s 到 %s", base, dest_dir)
    return extracted


def download_tool(tool_id: str, dest_dir: str) -> tuple[bool, str]:
    """下载单个工具到指定目录。

    Args:
        tool_id: 工具 id，须是 ``_TOOLS`` 的键。
        dest_dir: 解压目标目录，不存在时自动创建。
    Returns:
        ``(是否成功, 提示语或失败原因)``
    """
    spec = _TOOLS.get(tool_id)
    if not spec:
        return False, f"未知工具 {tool_id}"
    os.makedirs(dest_dir, exist_ok=True)
    wanted = spec["binaries"]

    url = None
    if spec.get("repo"):
        url = _github_latest_asset_url(spec["repo"], spec["asset"])
    if not url:
        url = spec.get("url") or spec.get("fallback")
    if not url:
        return False, f"{tool_id} 没有可用的下载源"

    try:
        got = _download_url(url, dest_dir, wanted)
        if not got:
            return False, f"压缩包里没有需要的二进制（{','.join(wanted)}）"
        return True, f"已下载 {', '.join(got)}"
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        # 下载中断、磁盘写失败、zip 损坏都归一为「失败 + 原因」交给界面提示
        log.warning("下载工具 %s 失败：%s", tool_id, exc)
        return False, str(exc)


def download_all_tools(dest_dir: str) -> dict[str, tuple[bool, str]]:
    """依次下载全部已知工具。

    Returns:
        形如 ``{工具 id: (是否成功, 消息)}`` 的字典；单个工具失败不影响其余。
    """
    return {tid: download_tool(tid, dest_dir) for tid in _TOOLS}


class ToolsDownloadWorker(QRunnable):
    """在 UI 线程之外下载**单个**工具的 worker。

    线程约定：``run()`` 在线程池线程执行，结果只经 :attr:`signals` 回传。
    """

    def __init__(self, tool_id: str, dest_dir: str):
        super().__init__()
        self.setAutoDelete(True)
        self.tool_id = tool_id
        self.dest_dir = dest_dir
        self.signals = DownloadSignals()

    def run(self) -> None:
        self.signals.started.emit(self.tool_id)
        ok, msg = download_tool(self.tool_id, self.dest_dir)
        self.signals.finished.emit(self.tool_id, ok, msg)


class AllDownloadSignals(QObject):
    """「下载全部工具」worker 的信号载体。

    信号：
    - ``started()`` —— 批量下载开始。
    - ``finished(dict)`` —— 全部结束，参数为 ``{工具 id: (是否成功, 消息)}``。
    """

    started = Signal()
    finished = Signal(dict)


class ToolsDownloadAllWorker(QRunnable):
    """在 UI 线程之外把**全部**已知工具下载到目标目录的 worker。

    线程约定：``run()`` 在线程池线程执行，结果只经 :attr:`signals` 回传。
    """

    def __init__(self, dest_dir: str):
        super().__init__()
        self.setAutoDelete(True)
        self.dest_dir = dest_dir
        self.signals = AllDownloadSignals()

    def run(self) -> None:
        self.signals.started.emit()
        result = download_all_tools(self.dest_dir)
        self.signals.finished.emit(result)
