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


class WorkerSignals(QObject):
    """Signals tunneled out of a worker thread."""

    started = Signal(str)
    progress = Signal(str, int)
    finished = Signal(str, bool, str)


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
        self.signals.started.emit(self.task.id)

        def on_progress(pct: int) -> None:
            self.signals.progress.emit(self.task.id, pct)

        returncode, log = run_conversion(
            self.task,
            self.ffmpeg_path,
            self.hw,
            on_progress=on_progress,
            cancel_event=self.cancel_event,
        )
        ok = returncode == 0
        self.signals.finished.emit(self.task.id, ok, (log or "") if not ok else "")


class ConversionManager(QObject):
    """Manages the batch queue: add, start, pause, resume, retry, remove, clear."""

    task_added = Signal(object)
    progress_updated = Signal(str, int)
    task_started = Signal(str)
    task_finished = Signal(str, bool, str)
    queue_changed = Signal()
    state_changed = Signal()

    def __init__(self, ffmpeg_path: Optional[str] = None):
        super().__init__()
        self.tasks: list[Task] = []
        self._events: dict[str, threading.Event] = {}
        self._pool = QThreadPool.globalInstance()
        self._max = 4
        self.ffmpeg_path = ffmpeg_path or find_ffmpeg(cfg.ffmpegSource.value)
        self.hw = detect_hw_accel(self.ffmpeg_path) if self.ffmpeg_path else {}
        self._running = False
        self._paused = False

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

    # -- adding -----------------------------------------------------------
    def add_files(
        self,
        paths: list[str],
        target_format: str,
        output_dir: Optional[str],
        use_gpu: bool,
    ) -> tuple[list[Task], list[str]]:
        """Add files as pending tasks. Returns (added_tasks, skipped_names)."""
        added: list[Task] = []
        skipped: list[str] = []
        default_out = Path(output_dir) if output_dir else None

        for raw in paths:
            src = Path(raw)
            if not src.exists():
                continue
            category = guess_category(str(src))
            if category is None:
                skipped.append(src.name)
                continue
            profile = PROFILES[target_format]
            if src.suffix.lower() == profile["ext"]:
                skipped.append(src.name)
                continue

            out_dir = default_out or src.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = self._unique_path(out_dir / (src.stem + profile["ext"]))

            task = Task(
                id=uuid.uuid4().hex[:12],
                input_path=str(src),
                output_path=str(out_path),
                target_format=target_format,
                category=category,
                use_gpu=bool(use_gpu and profile["category"] == "video"),
            )
            self.tasks.append(task)
            added.append(task)
            self.task_added.emit(task)

        self.queue_changed.emit()
        return added, skipped

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
        self._pool.start(worker)

    def _on_started(self, task_id: str) -> None:
        self.task_started.emit(task_id)

    def _on_progress(self, task_id: str, pct: int) -> None:
        task = self.get_task(task_id)
        if task:
            task.progress = pct
        self.progress_updated.emit(task_id, pct)

    def _on_finished(self, task_id: str, ok: bool, log: str) -> None:
        task = self.get_task(task_id)
        if task:
            task.status = Task.DONE if ok else Task.FAILED
            task.progress = 100 if ok else task.progress
            task.error = log
        self._events.pop(task_id, None)
        self.task_finished.emit(task_id, ok, log)

        if not self._paused:
            self._fill_slots()
        if self._running_count() == 0:
            self._running = False
        self.state_changed.emit()
