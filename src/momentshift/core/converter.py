"""单任务 ffmpeg 执行与实时进度上报。

职责边界：
- 做：拼装并执行 ffmpeg 命令、通过回调上报进度与日志、GPU 编码失败时自动回退 CPU。
- 不做：不依赖 Qt；不管理任务队列（由 queue 模块负责）。

依赖：core/platform、core/presets、core/logger；被依赖：core/queue、gui。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from . import proc_control
from .ffmpeg_progress import FFmpegProgressParser, probe_duration_ms
from .logger import get_logger
from .platform import popen_silent
from .presets import build_args

ProgressCallback = Callable[[int], None]
LogCallback = Callable[[str], None]

log = get_logger("converter")

# 判定「GPU 编码器本身不可用」的 ffmpeg 输出特征（ODD-05）。
# /29 这张表长到 9 条，混进了 ``not supported`` / ``invalid argument`` /
# ``unrecognized option`` 这类通用报错。后果是：用户把某个普通参数填错，也会被
# 判成「显卡不行」，白白再跑一遍 CPU 编码，耗时翻倍，最后报的还是同一个错。
# 收窄到 5 条，全部是**只有硬件编码器初始化失败才会出现**的串；同时在
# 开跑之前就用 :func:`hardware.encoder_usable` 探测一次编码器可用性，让绝大多数
# 「这台机器压根没有可用 GPU 编码器」的情况在第一次执行前就绕开，而不是靠事后
# 匹配错误文本补救。
# 注意：仍然是英文匹配。ffmpeg 的这几条 error 走的是内部固定串，不随系统语言
# 变化；真要换了版本导致失配，最坏结果也只是「不做 CPU 回退」，不会误判。
_HW_FAILURE_MARKERS: tuple[str, ...] = (
    "error while opening encoder",  # 通用：编码器打开失败
    "error creating a mfx session",  # Intel QSV
    "cannot load nvcuda",  # NVIDIA 驱动运行时缺失
    "no device available",  # AMF / VAAPI 找不到设备
    "failed to initialise",  # AMF 初始化失败
)


def _looks_like_hw_failure(err: str) -> bool:
    """错误文本是否指向「硬件编码器不可用」。"""
    lowered = (err or "").lower()
    return any(marker in lowered for marker in _HW_FAILURE_MARKERS)


# 取消时留给 ffmpeg 自行收尾的宽限期（秒）。
# 原值是 5 秒。ffmpeg 收到 terminate 后只需要写完当前帧并关掉输出文件，正常在
# 百毫秒级完成；5 秒纯属保守。批量取消 20 个任务时，最坏情况要串行等 100 秒，
# 期间进程和句柄一直挂着（RISK-05）。2 秒足够覆盖慢速磁盘，又不至于卡住用户。
_TERMINATE_GRACE_SEC = 2.0


def _stop(proc: subprocess.Popen, grace: float = _TERMINATE_GRACE_SEC) -> None:
    """尽快结束 ffmpeg：先礼后兵，且保证不抛异常。

    ``grace=0`` 表示不给宽限期，直接 kill（内部错误路径用，此时进程状态已不可信）。
    """
    try:
        if grace > 0:
            proc.terminate()
            try:
                proc.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg 未在 %.1fs 内退出，强制 kill", grace)
        proc.kill()
        proc.wait(timeout=grace or 1.0)
    except (OSError, subprocess.SubprocessError):
        # 进程可能已经自己退了。这里失败不影响调用方，句柄由 with 语句兜底关闭。
        log.debug("终止 ffmpeg 时进程已不存在")


def _usable_hw(ffmpeg_path: str, hw: dict, task) -> dict:
    """剔除 ``hw`` 里当前 ffmpeg 实际用不了的编码器（ODD-05）。

    返回一份新字典，不修改调用方传入的对象——``hw`` 通常是队列管理器复用的
    共享探测结果，就地改会影响后续所有任务。
    """
    from .hardware import encoder_usable

    filtered = dict(hw)
    for key in ("h264", "hevc"):
        encoder = filtered.get(key)
        if encoder and not encoder_usable(ffmpeg_path, encoder):
            log.warning(
                "GPU 编码器 %s 探测不可用，本次直接走 CPU：%s",
                encoder,
                getattr(task, "input_path", "?"),
            )
            filtered[key] = None
    return filtered


def run_conversion(
    task,
    ffmpeg_path: str,
    hw: dict | None = None,
    on_progress: ProgressCallback | None = None,
    on_log: LogCallback | None = None,
    cancel_event: object | None = None,
    on_stats=None,
    on_proc=None,
) -> tuple[int | None, str]:
    """执行一次转换。

    Args:
        task: 待转换任务，需含输入/输出路径与目标格式。
        ffmpeg_path: ffmpeg 可执行文件路径。
        hw: 硬件编码器映射；``None`` 等价于「无硬件加速」。
        on_progress: 进度回调，参数为 0..100 的百分比。
        on_log: 行日志回调，收到 ffmpeg 的每行输出。
        cancel_event: 置位后中止转换的事件对象。
        on_stats: 可选，收 :class:`~core.ffmpeg_progress.ProgressSnapshot`，
            用于展示编码速度与预计剩余时间（V0.8.21 新增）。
        on_proc: 可选，子进程刚启动时回调一次 ``on_proc(Popen)``；进程结束时
            再回调一次 ``on_proc(None)``。队列据此实现真正的暂停（psutil
            挂起整棵进程树，见 :mod:`core.proc_control`）。硬件降级重试会
            起第二个进程，因此这个回调**可能被调用多轮**，接收方要按
            「后来者覆盖」处理。
    Returns:
        ``(returncode, 错误文本)``，其中 returncode 含义为：
        ``0`` 成功；``None`` 被 ``cancel_event`` 取消；``<0`` 连 ffmpeg 都没能
        启动；``>0`` ffmpeg 以错误码退出（错误文本取自 stderr）。
    Notes:
        硬件编码失败（nvenc/qsv 等运行时不可用）时会自动降级用 CPU 重试一次，
        避免用户在「GPU 模式」下因驱动问题彻底转不了。
    """
    hw = hw or {}

    def _execute(args: list[str]) -> tuple[int | None, str]:
        cmd = [ffmpeg_path, *args]
        log.info("ffmpeg 命令：%s", " ".join(cmd))
        try:
            # popen_silent 已统一注入 CREATE_NO_WINDOW 与 utf-8/replace 编码。
            proc = popen_silent(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # 合并到 stdout，单流读取避免死锁
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            log.error("Failed to launch ffmpeg: %s", exc)
            return (-1, f"failed to launch ffmpeg: {exc}")

        if on_proc:
            on_proc(proc)  # V0.8.21 E4：交出句柄，队列暂停时挂起它

        # V0.8.21：分母走 ffprobe 预取。ffmpeg 的 -progress **不输出总时长**，
        # 旧实现在等一个永远不会来的 ``duration_ms`` 键，导致 out_time 分支
        # 永远进不去、中间进度一次都不上报，进度条只能靠 fake_progress 糊。
        parser = FFmpegProgressParser(
            duration_ms=probe_duration_ms(task.input_path, ffmpeg_path)
        )
        if parser.duration_ms:
            task.duration_ms = parser.duration_ms
        last_lines: list[str] = []
        # RISK-05：用 with 托管子进程。原先各条 return 路径都没有显式关掉
        # proc.stdout，管道句柄只能等 GC；取消分支还要先 terminate 再等满 5 秒
        # 才 kill。批量取消二三十个任务时，僵尸进程和句柄会短时间堆起来。
        # Popen 的 __exit__ 会关闭 stdin/stdout/stderr 并 wait()，保证无论从哪条
        # 路径退出都不留句柄。
        with proc:
            try:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        # V0.8.21 E4：进程可能正被 psutil 挂起，挂起态收不到
                        # terminate 的礼貌退出，必须先解挂再 _stop，否则一定
                        # 白等满一个宽限期才走到 kill。
                        proc_control.resume_then(proc)
                        _stop(proc)
                        if on_proc:
                            on_proc(None)
                        return (None, "canceled")

                    line = proc.stdout.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue

                    is_kv = "=" in line and not line.startswith(" ")
                    snap = parser.feed(line)
                    if snap is not None:
                        if on_progress and snap.pct is not None:
                            on_progress(snap.pct)
                        if on_stats:
                            on_stats(snap)
                        # 横幅补到的时长回写任务，供 UI 展示总时长
                        if parser.duration_ms and not task.duration_ms:
                            task.duration_ms = parser.duration_ms
                    if not is_kv:
                        if on_log:
                            on_log(line)
                        last_lines.append(line)
                        if len(last_lines) > 60:
                            last_lines.pop(0)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Error while reading ffmpeg output: %s", exc)
                proc_control.resume_then(proc)
                _stop(proc, grace=0)
                if on_proc:
                    on_proc(None)
                return (-3, f"internal error reading ffmpeg output: {exc}")

            returncode = proc.wait()

        if on_proc:
            on_proc(None)  # 进程已退出，撤销队列侧的句柄登记

        log.info(
            "ffmpeg finished: returncode=%s input=%s output=%s",
            returncode,
            task.input_path,
            task.output_path,
        )
        if returncode != 0:
            tail = "\n".join(last_lines[-30:])
            log.error("ffmpeg failed (rc=%s). Last output:\n%s", returncode, tail)
            return (returncode, tail or f"ffmpeg exited with code {returncode}")
        return (returncode, "")

    # ---- ODD-05：开跑前先确认 GPU 编码器真的能用 ----
    # 与其等 ffmpeg 跑挂了再靠 stderr 文本猜「是不是显卡的锅」，不如先问一句。
    # encoder_usable() 的结果按 (ffmpeg, 编码器) 缓存，一个进程内只探测一次，
    # 因此这层检查对批量任务几乎零开销。
    if getattr(task, "use_gpu", False):
        hw = _usable_hw(ffmpeg_path, hw, task)

    # ---- 首次执行（可能走 GPU 编码） ----
    try:
        args = build_args(task, hw)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("Failed to build ffmpeg arguments: %s", exc)
        return (-2, f"internal error building arguments: {exc}")

    returncode, err = _execute(args)

    # ---- ：GPU 编码失败 → CPU 回退一次（收窄后的兜底路径） ----
    gpu_active = bool(getattr(task, "use_gpu", False)) and bool(hw.get("h264") or hw.get("hevc"))
    if returncode not in (0, None) and gpu_active and err and _looks_like_hw_failure(err):
        log.warning("GPU encoder failed, retrying with CPU: %s", task.input_path)
        saved = getattr(task, "use_gpu", False)
        task.use_gpu = False
        try:
            returncode, err = _execute(build_args(task, {}))
        except Exception:
            log.exception("CPU fallback build failed")
            returncode, err = (returncode, err or "fallback failed")
        finally:
            task.use_gpu = saved

    if returncode != 0:
        return (returncode, err or f"ffmpeg exited with code {returncode}")

    # 记录产物大小，供界面做「转换前后体积对比」
    try:
        task.dst_size = Path(task.output_path).stat().st_size
    except OSError:
        task.dst_size = 0

    return (0, "")
