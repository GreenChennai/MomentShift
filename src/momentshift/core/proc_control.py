"""子进程挂起 / 恢复（V0.8.21 E4）。

职责边界：
- 做：把一个 :class:`subprocess.Popen` 连同它的整棵子进程树挂起 / 恢复；
  在 psutil 缺席或调用失败时安静降级。
- 不做：不创建进程、不杀进程（终止仍走各自模块的 ``_stop``）、不碰 Qt。

为什么需要它
------------
在此之前队列的「暂停」是**软暂停**——只是不再派发新任务，已经在跑的 ffmpeg
子进程照常吃满 CPU / GPU 跑完。用户按下暂停的真实诉求是「把机器让出来」，
软暂停在转一个 4K 长视频时等于没暂停。

参考 FFmpegFreeUI 的做法（它在 Windows 上走 ``NtSuspendProcess``），这里用
psutil 的 ``Process.suspend()`` / ``resume()``：Windows 下是挂起进程内所有
线程，Unix 下是 ``SIGSTOP`` / ``SIGCONT``，语义一致且跨平台。

三个必须注意的点
----------------
1. **要挂整棵树**。ffmpeg 自身通常没有子进程，但外部引擎（Real-ESRGAN、
   RIFE 等）会 fork 出干活的子进程；只挂父进程等于没挂。
2. **挂起后不要 terminate**。被 SIGSTOP 的进程收不到 SIGTERM 的处理机会，
   会一直僵在那；所以 :func:`resume` 必须先于任何取消 / 终止动作调用，
   :func:`resume_then` 就是给这个场景准备的。
3. **父进程会阻塞在 readline**。子进程挂起后管道自然不再有输出，读线程停在
   ``proc.stdout.readline()`` 上——这是期望行为，不是死锁，恢复后继续。

psutil 是可选依赖：没装就退化回旧的软暂停（返回 ``False``），不抛异常、
不影响任何既有功能。
"""

from __future__ import annotations

from .logger import get_logger

log = get_logger("proc_control")

_PSUTIL = None
_PSUTIL_TRIED = False


def _psutil():
    """惰性导入 psutil；未安装则返回 None（只警告一次）。"""
    global _PSUTIL, _PSUTIL_TRIED
    if not _PSUTIL_TRIED:
        _PSUTIL_TRIED = True
        try:
            import psutil  # type: ignore

            _PSUTIL = psutil
        except Exception:
            _PSUTIL = None
            log.warning("未安装 psutil，队列暂停将退化为「不派发新任务」的软暂停")
    return _PSUTIL


def available() -> bool:
    """psutil 是否可用（即能否做到真正的挂起）。"""
    return _psutil() is not None


def _tree(proc):
    """把 Popen 展开成 ``[父, 子, 孙...]`` 的 psutil.Process 列表。

    进程已退出 / 拿不到句柄时返回空列表，调用方据此静默跳过。
    """
    ps = _psutil()
    if ps is None or proc is None or proc.poll() is not None:
        return []
    try:
        parent = ps.Process(proc.pid)
    except Exception:
        return []  # 静默原因：进程刚好在这一瞬退出，属正常竞态
    try:
        # 先子后父地挂、先父后子地恢复都可以；这里统一返回「父在前」，
        # 由调用方决定顺序（挂起用倒序，恢复用正序，尽量减少孤儿窗口）。
        return [parent, *parent.children(recursive=True)]
    except Exception:
        return [parent]


def suspend(proc) -> bool:
    """挂起 ``proc`` 及其全部后代。

    Returns:
        只要至少挂起了一个进程就返回 True；psutil 不可用 / 进程已退出 /
        全部失败时返回 False（调用方应回退到软暂停语义）。
    """
    procs = _tree(proc)
    ok = False
    for p in reversed(procs):  # 先挂子再挂父，避免父在挂起瞬间又 fork 出新子
        try:
            p.suspend()
            ok = True
        except Exception:
            log.debug("挂起进程 %s 失败，忽略", getattr(p, "pid", "?"))
    return ok


def resume(proc) -> bool:
    """恢复 ``proc`` 及其全部后代；语义与 :func:`suspend` 对称。"""
    procs = _tree(proc)
    ok = False
    for p in procs:  # 先恢复父，让它能重新管住子
        try:
            p.resume()
            ok = True
        except Exception:
            log.debug("恢复进程 %s 失败，忽略", getattr(p, "pid", "?"))
    return ok


def resume_then(proc) -> None:
    """终止前的安全解挂。

    被挂起的进程收不到 SIGTERM / 不响应 ``TerminateProcess`` 之外的礼貌
    退出，取消任务前必须先恢复它，否则 ``_stop()`` 的 ``terminate() →
    wait(grace)`` 一定超时，白白多等一个宽限期才走到 ``kill()``。
    """
    try:
        resume(proc)
    except Exception:
        log.debug("终止前解挂失败，忽略")
