"""线程化的转换队列管理器。

职责边界：
- 做：持有任务列表与 QThreadPool，暴露 GUI 绑定的 Qt 信号，是引擎与 Qt 之间
  唯一的桥接层。
- 不做：不含具体的转码/压缩算法（由 converter / compressor 负责）。

依赖：core/advanced、core/config、core/converter、core/compressor、core/qt_compat；
被依赖：gui/convert_interface、gui/main_window。
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from . import advanced
from .config import cfg
from .converter import run_conversion
from .fake_progress import FakeProgressDriver, estimate_seconds
from .ffmpeg import find_ffmpeg
from .hardware import detect_hw_accel
from .logger import get_logger
from .models import Task
from .presets import PROFILES, guess_category
from .qt_compat import QObject, QRunnable, QThreadPool, Signal

log = get_logger("queue")


def compress_after_conversion(task: Task) -> None:
    """格式转换完成后按需再跑一遍压缩，构成「转换 → 压缩」两步管线。

    Args:
        task: 已完成转换的任务。只有 ``compress_enabled`` 为真且分类是
            ``image`` 时才真正执行，其余情况直接返回。
    Notes:
        后端为 ``auto``（默认）时由 compressor 按格式路由：
        png→oxipng / jpg→jpegoptim / gif→gifsicle / 其他→pillow。
        只在失败路径记日志——逐节点 info 在批量任务下会把日志文件淹没。
        压缩结果没变小时保留转换产物，仍记为压缩完成（节省量按 0 计）。
    """
    # compressor 只在这一个分支用得上，模块级导入会让 core.queue 无条件拖起
    # Pillow / 外部工具探测，故保持函数内延迟导入。
    from . import compressor

    if not task.compress_enabled or task.category != "image":
        return

    adv = task.adv or {}
    comp = adv.get("compress", {})
    if not isinstance(comp, dict):
        return

    fmt = Path(task.output_path).suffix.lower().lstrip(".")
    backend = comp.get("backend") or "auto"
    if backend == "auto":
        backend = compressor.default_backend(fmt)
    quality = int(comp.get("quality", 95))
    opts = dict(comp)

    # RISK-10：临时后缀复用 compressor 的常量，保证 cleanup_temp_files 能兜底认出它。
    tmp = str(task.output_path) + compressor.TMP_SUFFIX_COMPRESS
    try:
        ok = compressor.compress(task.output_path, tmp, fmt, quality, backend=backend, opts=opts)
        if ok and Path(tmp).exists():
            new_size = Path(tmp).stat().st_size
            old_size = task.dst_size or Path(task.output_path).stat().st_size
            if 0 < new_size < old_size:
                Path(tmp).replace(task.output_path)
                task.dst_size = new_size
            else:
                # 压缩没能变小：保留转换结果，仍记为压缩完成（节省 0）
                task.dst_size = old_size
            task.compress_done = True
        else:
            log.warning("压缩未产出结果，保留原文件：%s", Path(task.output_path).name)
    except Exception:
        log.exception("压缩异常：%s", task.output_path)
    finally:
        # RISK-10：替换成功时 tmp 已被 rename 消费，剩下的都是需要清掉的残留。
        try:
            if Path(tmp).exists():
                Path(tmp).unlink()
        except OSError:
            log.warning("临时文件清理失败：%s", tmp)


class WorkerSignals(QObject):
    """把 worker 线程内的状态变化透传到 GUI 线程的信号载体。

    线程约定：实例在 GUI 线程创建，信号在 worker 线程发出，Qt 的队列连接
    会自动切回 GUI 线程投递。

    信号：
    - ``started(str)`` —— 转换开始，参数为任务 id。
    - ``progress(str, int)`` —— 转换进度，参数为 ``(任务 id, 百分比)``。
    - ``finished(str, bool, str)`` —— 转换结束，参数为 ``(任务 id, 是否成功, 消息)``。
    - ``compress_started(str)`` / ``compress_progress(str, int)`` /
      ``compress_finished(str)`` —— 后置压缩阶段的对应信号。

    踩坑教训（别改）：signals 必须挂到 manager 作为 QObject parent，让 Qt 侧
    持有 C++ 对象。否则它会随 worker 的 Python 包装一起被 GC 回收，正在投递的
    队列连接打到已析构对象上，抛出 "WorkerSignals has been deleted" 直接崩溃。
    """

    started = Signal(str)
    progress = Signal(str, int)
    finished = Signal(str, bool, str)
    compress_started = Signal(str)
    compress_progress = Signal(str, int)
    compress_finished = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)


class ConversionWorker(QRunnable):
    """在线程池中执行单个 :class:`Task` 的转换 worker。

    线程约定：``run()`` 跑在线程池的 worker 线程，全部结果只经
    :attr:`signals` 回传，禁止在其中直接访问 Qt 控件。
    """

    def __init__(
        self,
        task: Task,
        ffmpeg_path: str,
        hw: dict,
        cancel_event: threading.Event,
        owner: QObject = None,
    ):
        super().__init__()
        self.setAutoDelete(True)
        self.task = task
        self.ffmpeg_path = ffmpeg_path
        self.hw = hw
        self.cancel_event = cancel_event
        self.signals = WorkerSignals(owner)  # parent=manager

    def run(self) -> None:
        try:
            self.signals.started.emit(self.task.id)

            def on_progress(pct: int) -> None:
                self.signals.progress.emit(self.task.id, pct)

            def on_log(line: str) -> None:
                get_logger("ffmpeg").info("%s", line)

            returncode, err = run_conversion(
                self.task,
                self.ffmpeg_path,
                self.hw,
                on_progress=on_progress,
                on_log=on_log,
                cancel_event=self.cancel_event,
            )
            ok = returncode == 0
            self.signals.finished.emit(self.task.id, ok, (err or "") if not ok else "")
        except Exception:
            get_logger("queue").exception("Worker crashed for task %s", self.task.id)
            self.signals.finished.emit(self.task.id, False, "internal worker error (see log)")


class CompressWorker(QRunnable):
    """在独立线程池中执行「转换后压缩」的 worker，与格式转换异步。

    线程约定：与 :class:`ConversionWorker` 相同，只经 :attr:`signals` 回传。

    单独开一个池而不是复用转换池，是为了让压缩不占转换槽位——压缩通常远快于
    转换，挤在同一个池里会让后面排队的转换任务白等。
    """

    def __init__(self, task: Task, owner: QObject = None):
        super().__init__()
        self.setAutoDelete(True)
        self.task = task
        self.signals = WorkerSignals(owner)  # parent=manager

    def run(self) -> None:
        try:
            self.signals.compress_started.emit(self.task.id)
            self.task.pre_compress_size = self.task.dst_size
            compress_after_conversion(self.task)
            self.signals.compress_finished.emit(self.task.id)
        except Exception:
            get_logger("queue").exception("CompressWorker crashed: %s", self.task.id)
            self.signals.compress_finished.emit(self.task.id)


class ConversionManager(QObject):
    """批量转换队列的调度中枢：增加、启动、暂停、恢复、重试、移除、清空。

    典型用法::

        mgr = ConversionManager()
        mgr.task_finished.connect(on_finished)
        mgr.add_files(paths, "mp4", out_dir, use_gpu=True)
        mgr.start()

    线程约定：所有公开方法都只在 GUI 线程调用；实际转码在 QThreadPool 的
    worker 线程执行，经 WorkerSignals 回传。

    信号：
    - ``task_added(object)`` —— 新任务入队。
    - ``progress_updated(str, int)`` —— 某任务进度变化。
    - ``task_started(str)`` / ``task_finished(str, bool, str)`` —— 转换起止。
    - ``compress_started(str)`` / ``compress_finished(str)`` —— 压缩阶段起止。
    - ``queue_changed()`` —— 队列内容变化，UI 需重新同步列表。
    - ``state_changed()`` —— 运行 / 暂停状态变化，UI 需刷新按钮。
    """

    task_added = Signal(object)
    progress_updated = Signal(str, int)
    task_started = Signal(str)
    task_finished = Signal(str, bool, str)
    compress_started = Signal(str)
    compress_progress = Signal(str, int)
    compress_finished = Signal(str)
    queue_changed = Signal()
    state_changed = Signal()

    def __init__(self, ffmpeg_path: str | None = None):
        super().__init__()
        self.tasks: list[Task] = []
        self._events: dict[str, threading.Event] = {}
        # 持有运行中的 worker，防止 Python GC 删除 signals 导致
        # "WorkerSignals has been deleted"（QRunnable 局部变量经典坑）
        self._workers: dict[str, ConversionWorker] = {}
        # ODD-14：已结束但暂缓释放的 worker。finished 信号是在 run() 尾声发出的，
        # 此刻 QRunnable::run 的栈帧还没退干净，立刻 drop 掉最后一个 Python 引用
        # 有概率让 C++ 侧对象先于 run() 返回被析构。旧实现用
        # QTimer.singleShot(1500) 硬扛这段窗口期——1500 是拍脑袋的魔法值，
        # 慢机器上未必够、快机器上纯属浪费。改成显式引用池：worker 一直被这里
        # 拿着，直到下一次调度事件（必然发生在 run() 已返回之后）才统一清空，
        # 既确定又不依赖时间。
        self._retired: list[ConversionWorker] = []
        self._pool = QThreadPool.globalInstance()
        self._compress_pool = QThreadPool()  # 独立压缩线程池
        self._compress_pool.setMaxThreadCount(3)
        self._max = 4
        self.ffmpeg_path = ffmpeg_path or find_ffmpeg(cfg.ffmpegSource.value)
        # 硬件加速探测放到后台线程：它要起 ffmpeg 子进程，耗时可达数百毫秒，
        # 放在构造函数里会直接卡住启动。探测完成前入队的任务本轮按无硬件加速跑。
        self.hw = {}
        self._hw_detected = False
        if self.ffmpeg_path:
            threading.Thread(target=self._detect_hw, daemon=True).start()
        self._running = False
        self._paused = False

        # v0.8.2 Bug3：假进度条驱动。无 ffmpeg ``duration_ms`` 的任务（图片 /
        # 音频 / 无元数据视频）真实进度回调不触发，进度长时间停 0% 然后瞬跳
        # 100%；此外压缩阶段会重置进度条造成「涨满又归零」。驱动器按 500ms
        # 节拍给每条运行中的任务发 5%..85% 的线性假进度，真实进度追上时取
        # ``max(fake, real)`` 自然接管；任务完成归 100%。详见
        # :mod:`core.fake_progress`。
        self._fake = FakeProgressDriver(self, interval_ms=500)
        self._fake.set_lookup(self.get_task)
        self._fake.progress_changed.connect(self._on_fake_progress)
        self._fake.compress_progress_changed.connect(self._on_fake_compress_progress)

    def _detect_hw(self) -> None:
        try:
            self.hw = detect_hw_accel(self.ffmpeg_path) if self.ffmpeg_path else {}
        except Exception:  # pragma: no cover - defensive
            self.hw = {}
        self._hw_detected = True
        log.info("硬件加速探测完成：%s", self.hw)

    # --- 属性 ---
    @property
    def has_ffmpeg(self) -> bool:
        """是否已定位到可用的 ffmpeg。"""
        return bool(self.ffmpeg_path)

    def refresh_ffmpeg(self) -> None:
        """用户装好 ffmpeg（如一键下载）后重新探测路径与硬件能力。"""
        self.ffmpeg_path = find_ffmpeg(cfg.ffmpegSource.value)
        self.hw = detect_hw_accel(self.ffmpeg_path) if self.ffmpeg_path else {}
        self.state_changed.emit()

    @property
    def is_running(self) -> bool:
        """队列当前是否处于运行中。"""
        return self._running

    @property
    def is_paused(self) -> bool:
        """队列当前是否已暂停。"""
        return self._paused

    def get_task(self, task_id: str) -> Task | None:
        """按 id 取任务，不存在返回 ``None``。"""
        return next((t for t in self.tasks if t.id == task_id), None)

    def counts(self) -> dict[str, int]:
        """按状态统计任务数，返回含 total/pending/running/done/failed/canceled 的字典。"""
        out = {
            "total": len(self.tasks),
            "pending": 0,
            "running": 0,
            "done": 0,
            "failed": 0,
            "canceled": 0,
        }
        for t in self.tasks:
            out[t.status] = out.get(t.status, 0) + 1
        return out

    def pending_same_format(self) -> list[Task]:
        """返回「源扩展名与目标扩展名相同」的待处理任务（如 png → png）。

        Returns:
            命中的任务列表；没有则为空列表。
        Notes:
            同格式转换是允许的，但调用方应在启动前提示用户确认，避免用户是
            误选目标格式。
        """
        out = []
        for t in self.tasks:
            if t.status != Task.PENDING:
                continue
            ext = Path(t.input_path).suffix.lower()
            if ext and ext == PROFILES[t.target_format]["ext"]:
                out.append(t)
        return out

    # --- 入队 ---
    def add_files(
        self,
        paths: list[str],
        target_format: str,
        output_dir: str | None,
        use_gpu: bool,
        output_mode: str = "fixed",
        suffix: str = "",
        compress_enabled: bool = False,
    ) -> tuple[list[Task], list[str]]:
        """把文件批量加入队列，生成待处理任务。

        Args:
            paths: 源文件路径列表，不存在的路径会被静默跳过。
            target_format: 目标格式键，须是 ``PROFILES`` 的键。
            output_dir: ``output_mode="fixed"`` 时的输出目录。
            use_gpu: 是否允许硬件编码；只对视频类目标生效。
            output_mode: ``fixed`` 输出到 ``output_dir``；``same`` 输出到源
                文件旁，并在主名后追加 ``suffix`` 以免覆盖原文件。
            suffix: ``output_mode="same"`` 时追加到文件主名后的后缀。
            compress_enabled: 转换完成后是否再跑一遍压缩。
        Returns:
            ``(新增的任务列表, 因无法识别分类而跳过的文件名列表)``
        Notes:
            当该分类开启了高级选项「合并为一个文件」且待入队文件多于一个时，
            只创建**一个**合并任务，而不是每个文件一个任务。
            同格式转换（如 png → png）允许通过，提示用户的责任在调用方。
        """
        added: list[Task] = []
        skipped: list[str] = []
        default_out = Path(output_dir) if (output_dir and output_mode == "fixed") else None

        existing = [p for p in paths if Path(p).exists()]
        if not existing:
            return added, skipped
        category = guess_category(existing[0])

        # v0.8.3「仅提取音频」：把目标格式改写为所选音频格式（mp3/wav/...）。
        # 输出扩展名、use_gpu、队列展示的目标格式全部随 PROFILES 自动对齐。
        if category and advanced.is_extract_audio_enabled(category):
            audio_fmt = str(
                advanced.get("video").get("audio_format", "mp3") or "mp3"
            ).lower().lstrip(".")
            if audio_fmt in PROFILES:
                target_format = audio_fmt

        # --- 合并模式：只产出一个合并任务 ---
        # 仅提取音频时跳过合并（音频提取是逐文件独立输出，concat 语义不适用）。
        if (
            category
            and advanced.is_merge_enabled(category)
            and len(existing) > 1
            and not advanced.is_extract_audio_enabled(category)
        ):
            profile = PROFILES[target_format]
            if output_mode == "same":
                out_dir = Path(existing[0]).parent
                stem = Path(existing[0]).stem + (suffix or "") + "_merged"
            else:
                out_dir = default_out or Path(existing[0]).parent
                stem = Path(existing[0]).stem + "_merged"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._unique_path(out_dir / (stem + profile["ext"]))
            task = Task(
                id=uuid.uuid4().hex[:12],
                input_path=existing[0],
                output_path=str(out_path),
                target_format=target_format,
                category=category,
                use_gpu=False,
                # Q3：入队时深拷贝定格高级参数。此处必须是深拷贝——浅拷贝下
                # image.compress 子字典仍与面板共享引用，用户改面板会串改已入队任务。
                adv=advanced.snapshot(category),
                compress_enabled=compress_enabled,
                merge=True,
                input_paths=list(existing),
            )
            task.src_size = sum(self._safe_size(p) for p in existing)
            self.tasks.append(task)
            added.append(task)
            log.info(
                "add_files(merge): task=%s compress_enabled=%s", task.id, task.compress_enabled
            )
            self.task_added.emit(task)
            self.queue_changed.emit()
            return added, skipped

        # --- 常规模式：每个文件一个任务 ---
        for raw in existing:
            src = Path(raw)
            category = guess_category(str(src))
            if category is None:
                skipped.append(src.name)
                continue
            profile = PROFILES[target_format]

            if output_mode == "same":
                out_dir = src.parent
                stem = src.stem + (suffix or "")
            else:
                out_dir = default_out or src.parent
                stem = src.stem
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._unique_path(out_dir / (stem + profile["ext"]))

            task = Task(
                id=uuid.uuid4().hex[:12],
                input_path=str(src),
                output_path=str(out_path),
                target_format=target_format,
                category=category,
                use_gpu=bool(use_gpu and profile["category"] == "video"),
                # Q3：同上，入队即定格，后续改面板不影响已排队任务。
                adv=advanced.snapshot(category),
                compress_enabled=compress_enabled,
            )
            task.src_size = self._safe_size(str(src))
            self.tasks.append(task)
            added.append(task)
            log.info(
                "add_files: task=%s cat=%s fmt=%s compress_enabled=%s",
                task.id,
                task.category,
                task.target_format,
                task.compress_enabled,
            )
            self.task_added.emit(task)

        self.queue_changed.emit()
        return added, skipped

    @staticmethod
    def _safe_size(path: str) -> int:
        """取文件大小，读不到（不存在 / 无权限）时按 0 计，不打断入队。"""
        try:
            return Path(path).stat().st_size
        except OSError:
            return 0

    # --- 目标格式（重）指派 ---
    def set_task_target(self, task_id: str, fmt: str) -> None:
        """改写某个任务的目标格式，并按新扩展名重算输出路径。

        Notes:
            若任务已处于终态（完成 / 失败 / 已取消），会被重置回待处理，
            以便用新格式重跑。
        """
        task = self.get_task(task_id)
        if not task or task.target_format == fmt:
            return
        profile = PROFILES.get(fmt)
        if not profile:
            return
        task.target_format = fmt
        out_dir = Path(task.output_path).parent
        new_ext = profile["ext"]
        task.output_path = str(self._unique_path(out_dir / (Path(task.input_path).stem + new_ext)))
        if task.status in (Task.DONE, Task.FAILED, Task.CANCELED):
            task.status = Task.PENDING
            task.progress = 0
            task.error = ""
        self.queue_changed.emit()

    def set_targets_by_category(self, targets: dict[str, str]) -> None:
        """按「分类 → 目标格式」映射批量改写队列中任务的目标格式。

        Args:
            targets: 形如 ``{"video": "mp4", "image": "png"}`` 的映射。
        Notes:
            正在运行的任务会被跳过——改写运行中任务的输出路径会让产物落到
            与 worker 预期不一致的位置。
        """
        for t in self.tasks:
            if t.status == Task.RUNNING:
                continue
            fmt = targets.get(t.category)
            if fmt:
                self.set_task_target(t.id, fmt)
        self.queue_changed.emit()

    @staticmethod
    def _unique_path(path: Path) -> Path:
        """若目标路径已存在，则在主名后追加 ``_1`` / ``_2`` … 直到不冲突。"""
        if not path.exists():
            return path
        i = 1
        while True:
            candidate = path.parent / f"{path.stem}_{i}{path.suffix}"
            if not candidate.exists():
                return candidate
            i += 1

    # --- 运行控制 ---
    def start(self) -> bool:
        """开始（或继续）处理全部待处理任务。

        Returns:
            成功启动返回 ``True``；未找到 ffmpeg 而无法启动返回 ``False``。
        """
        if not self.ffmpeg_path:
            return False
        self._paused = False
        self._running = True
        self._compute_max_threads()
        self._fill_slots()
        self.state_changed.emit()
        return True

    def pause(self) -> None:
        """暂停队列：已在跑的任务继续跑完，但不再启动新任务。"""
        self._paused = True
        self.state_changed.emit()

    def resume(self) -> None:
        """从暂停状态恢复调度；未处于暂停时为空操作。"""
        if not self._paused:
            return
        self._paused = False
        self._running = True
        self._compute_max_threads()
        self._fill_slots()
        self.state_changed.emit()

    def retry(self, task_id: str) -> None:
        """把某个任务重置为待处理并重新排队；队列没在跑时顺带启动。"""
        task = self.get_task(task_id)
        if not task:
            return
        task.status = Task.PENDING
        task.error = ""
        task.progress = 0
        # v0.8.2 Bug3：清理残留的假进度状态，下一轮 _launch 会重新 start。
        self._fake.stop(task_id)
        if not self._running:
            self.start()
        else:
            self._fill_slots()
        self.queue_changed.emit()

    def remove(self, task_id: str) -> None:
        """从队列中移除任务；若正在运行会先发出取消信号。"""
        self.cancel_task(task_id)
        self.tasks = [t for t in self.tasks if t.id != task_id]
        self.queue_changed.emit()

    def clear(self) -> None:
        """清空整个队列，并给所有在跑任务置取消标志。"""
        for event in self._events.values():
            event.set()
        # v0.8.2 Bug3：清空时一并停掉所有假进度追踪，避免定时器空转。
        for task in self.tasks:
            self._fake.stop(task.id)
        self.tasks.clear()
        self._running = False
        self._paused = False
        self.queue_changed.emit()
        self.state_changed.emit()

    def cancel_task(self, task_id: str) -> None:
        """给指定任务置取消标志，worker 会在下一个检查点自行退出。"""
        event = self._events.get(task_id)
        if event:
            event.set()
        task = self.get_task(task_id)
        if task and task.status == Task.RUNNING:
            task.status = Task.CANCELED
        # v0.8.2 Bug3：取消的任务不再上报进度（UI 也不该再更新）。
        self._fake.stop(task_id)

    # --- 内部实现 ---
    def _compute_max_threads(self) -> None:
        """根据待处理任务是否用 GPU，动态决定转换线程池的并发上限。"""
        gpu_pending = any(t.use_gpu for t in self.tasks if t.status == Task.PENDING)
        if gpu_pending:
            # 显卡驱动通常会把硬件编码会话串行化，开太多并发只会增加切换开销
            self._max = 2
        else:
            self._max = max(1, min(int(os.cpu_count() or 4), 8))
        self._pool.setMaxThreadCount(self._max)

    def _running_count(self) -> int:
        """返回当前处于运行中状态的任务数。"""
        return sum(1 for t in self.tasks if t.status == Task.RUNNING)

    def _drain_retired(self) -> None:
        """释放上一批已结束的 worker（ODD-14）。

        调用点都在 GUI 线程的后续事件里，此时对应的 ``run()`` 早已返回，
        丢引用是安全的。
        """
        self._retired.clear()

    def _fill_slots(self) -> None:
        """把空闲的并发槽位补满待处理任务；暂停状态下不启动任何新任务。"""
        self._drain_retired()
        if self._paused:
            return
        while self._running_count() < self._max:
            pending = [t for t in self.tasks if t.status == Task.PENDING]
            if not pending:
                break
            self._launch(pending[0])

    def _launch(self, task: Task) -> None:
        """为单个任务创建 worker、接好信号并投递到转换线程池。"""
        task.status = Task.RUNNING
        task.progress = 0
        task.error = ""
        event = threading.Event()
        self._events[task.id] = event
        worker = ConversionWorker(task, self.ffmpeg_path, self.hw, event, self)
        worker.signals.started.connect(self._on_started)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.compress_started.connect(self.compress_started)
        worker.signals.compress_finished.connect(self.compress_finished)
        self._workers[task.id] = worker  # 持有引用防 GC
        self._pool.start(worker)
        # v0.8.2 Bug3：启动假进度条。任务刚被派发就开始涨（用户感知的
        # 「任务开始」），即使 ffmpeg 还没吐出第一个 duration_ms 也不会卡 0%。
        self._fake.start(task.id, estimate_seconds(task))

    def _on_started(self, task_id: str) -> None:
        """worker 报告开始 → 中继 ``task_started``。"""
        self.task_started.emit(task_id)

    def _on_progress(self, task_id: str, pct: int) -> None:
        """worker 报告进度 → 交给假进度条合并后发出 ``progress_updated``。

        合并策略：``max(fake_now, real_pct)``。真实进度追上假进度后自然
        接管显示；两者皆 0 时由假进度条驱动 5%..85% 线性曲线。
        """
        self._fake.merge(task_id, pct)

    def _on_fake_progress(self, task_id: str, pct: int) -> None:
        """假进度条 → 写回任务对象并中继 ``progress_updated``。"""
        task = self.get_task(task_id)
        if task:
            task.progress = pct
        self.progress_updated.emit(task_id, pct)

    def _on_fake_compress_progress(self, task_id: str, pct: int) -> None:
        """压缩阶段的假进度 → 中继 ``compress_progress``。"""
        self.compress_progress.emit(task_id, pct)

    def _on_finished(self, task_id: str, ok: bool, log: str) -> None:
        """转换阶段结束的回调：更新任务状态、释放 worker、按需转入压缩阶段。

        Args:
            task_id: 任务 id。
            ok: 转换是否成功。
            log: 失败原因或空串。
        Notes:
            无论是否还要压缩，都先标记「已完成」并发出 ``task_finished``，让
            队列 UI 先亮绿；随后才进入压缩阶段（由 ``compress_started`` 切成
            「压缩中」）。这样用户看到的状态序列才是
            等待中 → 已完成 → 压缩中 → 压缩完成，而不会卡在「转换中」不动。
        """
        task = self.get_task(task_id)
        need_compress = bool(task and ok and task.compress_enabled)

        if task:
            task.error = log
            task.status = Task.DONE if ok else Task.FAILED

        self._events.pop(task_id, None)
        # ODD-14：worker 转入 _retired 暂存，等下一次调度事件再释放（见 __init__ 注释）。
        done_worker = self._workers.pop(task_id, None)
        if done_worker is not None:
            self._retired.append(done_worker)

        # v0.8.2 Bug3：转换阶段结束 → 假进度条归 100% 并发出最终进度，
        # 再发 task_finished 让 UI 切绿胶囊。失败时停追踪但不强制改 progress，
        # 保留假进度条最后位置（UI 切红胶囊）。
        if ok:
            self._fake.finish(task_id)
        else:
            self._fake.stop(task_id)
        self.task_finished.emit(task_id, ok, log)

        if need_compress:
            # 压缩走独立线程池，COMPRESSING 状态不占用转换槽位
            task.status = Task.COMPRESSING
            task.pre_compress_size = task.dst_size
            cw = CompressWorker(task, self)  # signals parent=manager
            cw.signals.compress_started.connect(self.compress_started)
            cw.signals.compress_finished.connect(self._on_compress_finished)
            self._compress_pool.start(cw)
            # v0.8.2 Bug3：启动压缩阶段的假进度（5%..85%→100%），避免
            # 压缩过程进度条一直停 0%、完成时瞬跳 100%。新通道以「compress」
            # 标记，与转换阶段的进度通道互不干扰。
            self._fake.start(task.id, estimate_seconds(task, "compress"), channel="compress")

        if not self._paused:
            self._fill_slots()  # 内部会先 _drain_retired
        else:
            # 暂停时不再调度，_retired 不会有人来清，这里补一刀免得越攒越多。
            self._drain_retired()
        if self._running_count() == 0 and not need_compress:
            self._running = False
        self.state_changed.emit()

    def _on_compress_finished(self, task_id: str) -> None:
        """压缩阶段结束 → 回到 DONE 并中继 ``compress_finished``。

        不再重复发 ``task_finished``：转换结束时已经发过一次，重复发送会让
        队列 UI 把黄/蓝的压缩状态又刷回绿色。

        v0.8.2 Bug3：归 100% 让压缩阶段假进度条收尾。
        """
        task = self.get_task(task_id)
        if task:
            task.status = Task.DONE
        self._fake.finish(task_id)
        self.compress_finished.emit(task_id)
        if self._running_count() == 0:
            self._running = False
        self.state_changed.emit()
