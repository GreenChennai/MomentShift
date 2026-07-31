"""Threaded conversion queue manager.

Owns the task list and a :class:`QThreadPool`. Exposes Qt signals the GUI binds
to. The engine itself (converter / presets) is UI-free; this class is the only
piece that bridges the engine to Qt.
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Optional

from .qt_compat import QObject, Signal, QRunnable, QThreadPool
from .models import Task
from .presets import PROFILES, TARGET_GROUPS, build_args, guess_category
from .converter import run_conversion
from .ffmpeg import find_ffmpeg
from .config import cfg
from .hardware import detect_hw_accel
from .logger import get_logger
from . import advanced

log = get_logger("queue")


def compress_after_conversion(task: "Task") -> None:
    """v0.6.0：格式转换后按需运行压缩（两步管线）。

    v0.7.0：移除 v0.6.2 加入的逐节点 log.info，只保留失败路径的日志。
    后端为 ``auto``（默认）时由 compressor 按格式路由：
    png→oxipng / jpg→jpegoptim / 其他→pillow。
    """
    from pathlib import Path as _Path
    from . import compressor

    if not task.compress_enabled or task.category != "image":
        return

    adv = task.adv or {}
    comp = adv.get("compress", {})
    if not isinstance(comp, dict):
        return

    fmt = _Path(task.output_path).suffix.lower().lstrip(".")
    backend = comp.get("backend") or "auto"
    if backend == "auto":
        backend = compressor.default_backend(fmt)
    quality = int(comp.get("quality", 95))
    opts = dict(comp)

    tmp = str(task.output_path) + ".cmp.tmp"
    try:
        ok = compressor.compress(task.output_path, tmp, fmt, quality,
                                 backend=backend, opts=opts)
        if ok and _Path(tmp).exists():
            new_size = _Path(tmp).stat().st_size
            old_size = task.dst_size or _Path(task.output_path).stat().st_size
            if 0 < new_size < old_size:
                _Path(tmp).replace(task.output_path)
                task.dst_size = new_size
            else:
                # 压缩没能变小：保留转换结果，仍记为压缩完成（节省 0）
                _Path(tmp).unlink()
                task.dst_size = old_size
            task.compress_done = True
        else:
            log.warning("压缩未产出结果，保留原文件：%s", _Path(task.output_path).name)
            if _Path(tmp).exists():
                _Path(tmp).unlink()
    except Exception:
        log.exception("压缩异常：%s", task.output_path)
        if _Path(tmp).exists():
            _Path(tmp).unlink()


class WorkerSignals(QObject):
    """Signals tunneled out of a worker thread."""

    started = Signal(str)
    progress = Signal(str, int)
    finished = Signal(str, bool, str)
    compress_started = Signal(str)   # v0.6.5：压缩开始
    compress_progress = Signal(str, int)  # v0.6.5：压缩进度
    compress_finished = Signal(str)  # v0.6.5：压缩完成


class ConversionWorker(QRunnable):
    """Runs a single :class:`Task` inside the thread pool."""

    def __init__(self, task: Task, ffmpeg_path: str, hw: dict, cancel_event: threading.Event):
        super().__init__()
        self.setAutoDelete(True)
        self.task = task
        self.ffmpeg_path = ffmpeg_path
        self.hw = hw
        self.cancel_event = cancel_event
        self.signals = WorkerSignals()

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
    """v0.6.7：独立压缩线程（与格式转换异步）。"""

    def __init__(self, task: Task):
        super().__init__()
        self.setAutoDelete(True)
        self.task = task
        self.signals = WorkerSignals()

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
    """Manages the batch queue: add, start, pause, resume, retry, remove, clear."""

    task_added = Signal(object)
    progress_updated = Signal(str, int)
    task_started = Signal(str)
    task_finished = Signal(str, bool, str)
    compress_started = Signal(str)   # v0.6.5
    compress_finished = Signal(str)  # v0.6.5
    queue_changed = Signal()
    state_changed = Signal()

    def __init__(self, ffmpeg_path: Optional[str] = None):
        super().__init__()
        self.tasks: list[Task] = []
        self._events: dict[str, threading.Event] = {}
        self._pool = QThreadPool.globalInstance()
        self._compress_pool = QThreadPool()  # v0.6.7：独立压缩线程池
        self._compress_pool.setMaxThreadCount(3)
        self._max = 4
        self.ffmpeg_path = ffmpeg_path or find_ffmpeg(cfg.ffmpegSource.value)
        # Defer hardware-accel probing to a background thread so it never blocks
        # app startup (the subprocess can take hundreds of ms). Tasks that start
        # before detection finishes simply run without HW accel for that run.
        self.hw = {}
        self._hw_detected = False
        if self.ffmpeg_path:
            threading.Thread(target=self._detect_hw, daemon=True).start()
        self._running = False
        self._paused = False

    def _detect_hw(self) -> None:
        try:
            self.hw = detect_hw_accel(self.ffmpeg_path) if self.ffmpeg_path else {}
        except Exception:  # pragma: no cover - defensive
            self.hw = {}
        self._hw_detected = True
        log.info("hardware-accel detection complete: %s", self.hw)

    # -- properties -------------------------------------------------------
    @property
    def has_ffmpeg(self) -> bool:
        return bool(self.ffmpeg_path)

    def refresh_ffmpeg(self) -> None:
        """Re-detect ffmpeg after the user installs it (e.g. one-click download)."""
        self.ffmpeg_path = find_ffmpeg(cfg.ffmpegSource.value)
        self.hw = detect_hw_accel(self.ffmpeg_path) if self.ffmpeg_path else {}
        self.state_changed.emit()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def get_task(self, task_id: str) -> Optional[Task]:
        return next((t for t in self.tasks if t.id == task_id), None)

    def counts(self) -> dict[str, int]:
        out = {"total": len(self.tasks), "pending": 0, "running": 0,
               "done": 0, "failed": 0, "canceled": 0}
        for t in self.tasks:
            out[t.status] = out.get(t.status, 0) + 1
        return out

    def pending_same_format(self) -> list[Task]:
        """Return pending tasks whose source extension equals the target ext.

        Same-format conversions are allowed but the caller should warn before
        starting the run (e.g. png -> png).
        """
        out = []
        for t in self.tasks:
            if t.status != Task.PENDING:
                continue
            ext = Path(t.input_path).suffix.lower()
            if ext and ext == PROFILES[t.target_format]["ext"]:
                out.append(t)
        return out

    # -- adding -----------------------------------------------------------
    def add_files(
        self,
        paths: list[str],
        target_format: str,
        output_dir: Optional[str],
        use_gpu: bool,
        output_mode: str = "fixed",
        suffix: str = "",
        compress_enabled: bool = False,
    ) -> tuple[list[Task], list[str]]:
        """Add files as pending tasks. Returns (added_tasks, skipped_names).

        ``output_mode`` is either ``"fixed"`` (use ``output_dir``) or
        ``"same"`` (place the output next to the source file, appending
        ``suffix`` to the file stem to avoid clobbering the original).
        Same-format conversions (e.g. png -> png) are allowed; the caller is
        responsible for warning the user before starting.

        If the advanced "merge into one file" option is enabled for the
        category and there is more than one file, a single *merge* task is
        created instead of one task per file.
        """
        added: list[Task] = []
        skipped: list[str] = []
        default_out = Path(output_dir) if (output_dir and output_mode == "fixed") else None

        existing = [p for p in paths if Path(p).exists()]
        if not existing:
            return added, skipped
        category = guess_category(existing[0])

        # --- merge mode: one combined task ------------------------------
        if category and advanced.is_merge_enabled(category) and len(existing) > 1:
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
                adv=dict(advanced.get(category)),
                compress_enabled=compress_enabled,
                merge=True,
                input_paths=list(existing),
            )
            task.src_size = sum(self._safe_size(p) for p in existing)
            self.tasks.append(task)
            added.append(task)
            log.info("add_files(merge): task=%s compress_enabled=%s",
                     task.id, task.compress_enabled)
            self.task_added.emit(task)
            self.queue_changed.emit()
            return added, skipped

        # --- one task per file ------------------------------------------
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
                adv=dict(advanced.get(category)),
                compress_enabled=compress_enabled,
            )
            task.src_size = self._safe_size(str(src))
            self.tasks.append(task)
            added.append(task)
            log.info("add_files: task=%s cat=%s fmt=%s compress_enabled=%s",
                     task.id, task.category, task.target_format, task.compress_enabled)
            self.task_added.emit(task)

        self.queue_changed.emit()
        return added, skipped

    @staticmethod
    def _safe_size(path: str) -> int:
        try:
            return Path(path).stat().st_size
        except OSError:
            return 0

    # -- target (re)assignment ------------------------------------------
    def set_task_target(self, task_id: str, fmt: str) -> None:
        """Change a task's target format and recompute its output path."""
        task = self.get_task(task_id)
        if not task or task.target_format == fmt:
            return
        profile = PROFILES.get(fmt)
        if not profile:
            return
        task.target_format = fmt
        out_dir = Path(task.output_path).parent
        new_ext = profile["ext"]
        task.output_path = str(
            self._unique_path(out_dir / (Path(task.input_path).stem + new_ext))
        )
        if task.status in (Task.DONE, Task.FAILED, Task.CANCELED):
            task.status = Task.PENDING
            task.progress = 0
            task.error = ""
        self.queue_changed.emit()

    def set_targets_by_category(self, targets: dict[str, str]) -> None:
        """Apply a per-category target format to queued tasks (skip running)."""
        for t in self.tasks:
            if t.status == Task.RUNNING:
                continue
            fmt = targets.get(t.category)
            if fmt:
                self.set_task_target(t.id, fmt)
        self.queue_changed.emit()

    @staticmethod
    def _unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        i = 1
        while True:
            candidate = path.parent / f"{path.stem}_{i}{path.suffix}"
            if not candidate.exists():
                return candidate
            i += 1

    # -- controls ---------------------------------------------------------
    def start(self) -> bool:
        """Begin (or resume) processing all pending tasks."""
        if not self.ffmpeg_path:
            return False
        self._paused = False
        self._running = True
        self._compute_max_threads()
        self._fill_slots()
        self.state_changed.emit()
        return True

    def pause(self) -> None:
        """Halt the queue. In-flight tasks run to completion; no new launches."""
        self._paused = True
        self.state_changed.emit()

    def resume(self) -> None:
        if not self._paused:
            return
        self._paused = False
        self._running = True
        self._compute_max_threads()
        self._fill_slots()
        self.state_changed.emit()

    def retry(self, task_id: str) -> None:
        task = self.get_task(task_id)
        if not task:
            return
        task.status = Task.PENDING
        task.error = ""
        task.progress = 0
        if not self._running:
            self.start()
        else:
            self._fill_slots()
        self.queue_changed.emit()

    def remove(self, task_id: str) -> None:
        self.cancel_task(task_id)
        self.tasks = [t for t in self.tasks if t.id != task_id]
        self.queue_changed.emit()

    def clear(self) -> None:
        for event in self._events.values():
            event.set()
        self.tasks.clear()
        self._running = False
        self._paused = False
        self.queue_changed.emit()
        self.state_changed.emit()

    def cancel_task(self, task_id: str) -> None:
        event = self._events.get(task_id)
        if event:
            event.set()
        task = self.get_task(task_id)
        if task and task.status == Task.RUNNING:
            task.status = Task.CANCELED

    # -- internals --------------------------------------------------------
    def _compute_max_threads(self) -> None:
        gpu_pending = any(
            t.use_gpu for t in self.tasks if t.status == Task.PENDING
        )
        if gpu_pending:
            self._max = 2  # GPU encoders are typically serialized by the driver
        else:
            self._max = max(1, min(int(os.cpu_count() or 4), 8))
        self._pool.setMaxThreadCount(self._max)

    def _running_count(self) -> int:
        return sum(1 for t in self.tasks if t.status == Task.RUNNING)

    def _fill_slots(self) -> None:
        if self._paused:
            return
        while self._running_count() < self._max:
            pending = [t for t in self.tasks if t.status == Task.PENDING]
            if not pending:
                break
            self._launch(pending[0])

    def _launch(self, task: Task) -> None:
        task.status = Task.RUNNING
        task.progress = 0
        task.error = ""
        event = threading.Event()
        self._events[task.id] = event
        worker = ConversionWorker(task, self.ffmpeg_path, self.hw, event)
        worker.signals.started.connect(self._on_started)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.compress_started.connect(self.compress_started)
        worker.signals.compress_finished.connect(self.compress_finished)
        self._pool.start(worker)

    def _on_started(self, task_id: str) -> None:
        self.task_started.emit(task_id)

    def _on_progress(self, task_id: str, pct: int) -> None:
        task = self.get_task(task_id)
        if task:
            task.progress = pct
        self.progress_updated.emit(task_id, pct)

    def _on_finished(self, task_id: str, ok: bool, log: str) -> None:
        """转换结束。

        v0.7.0：无论是否还要压缩，都先把任务标记为「已完成」并发出
        ``task_finished``，让队列 UI 先亮绿色；随后再进入压缩阶段
        （UI 由 ``compress_started`` 切换成黄色「压缩中」）。这样状态序列
        才是 等待中 → 已完成 → 压缩中 → 压缩完成。
        """
        task = self.get_task(task_id)
        need_compress = bool(task and ok and task.compress_enabled)

        if task:
            task.progress = 100 if ok else task.progress
            task.error = log
            task.status = Task.DONE if ok else Task.FAILED

        self._events.pop(task_id, None)
        self.task_finished.emit(task_id, ok, log)

        if need_compress:
            # 压缩在独立线程池执行，COMPRESSING 不占用转换槽位（v0.6.8）
            task.status = Task.COMPRESSING
            task.pre_compress_size = task.dst_size
            cw = CompressWorker(task)
            cw.signals.compress_started.connect(self.compress_started)
            cw.signals.compress_finished.connect(self._on_compress_finished)
            self._compress_pool.start(cw)

        if not self._paused:
            self._fill_slots()
        if self._running_count() == 0 and not need_compress:
            self._running = False
        self.state_changed.emit()

    def _on_compress_finished(self, task_id: str) -> None:
        """压缩阶段结束 → 回到 DONE 并中继 ``compress_finished``。

        不再重复发 ``task_finished``：转换结束时已经发过一次，重复发送会让
        队列 UI 把黄/蓝的压缩状态又刷回绿色。
        """
        task = self.get_task(task_id)
        if task:
            task.status = Task.DONE
        self.compress_finished.emit(task_id)
        if self._running_count() == 0:
            self._running = False
        self.state_changed.emit()
