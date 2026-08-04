"""假进度条驱动（v0.8.2 Bug3）。

背景
====
两大队列的进度反馈都有问题：

① 无 ffmpeg ``duration_ms`` 的任务（图片、音频、无元数据视频），
   :func:`core.converter.run_conversion` 的 ``on_progress`` 从不触发，
   进度长时间停在 0%，任务完成瞬间跳到 100%。

② 转换完成后若开启了「转换后压缩」，
   :class:`core.queue.ConversionManager._on_finished` 会把 ``progress``
   写入 100、UI 切成「已完成（绿）」，紧接着 ``compress_started`` 又把
   进度条重置为 0、UI 切成「压缩中（黄）」—— 用户看到的是「涨满一条
   又归零涨第二条」。

设计
====
给每条运行中的任务维护一个**假进度状态机**：

* ``start(task_id, estimate_seconds)`` —— 任务开始置 5%，启动计时。
* ``tick(task_id)`` —— 按 ``(now - start) / estimate`` 线性映射 5%..85%。
  超过预计时间后封顶 85% 不再涨（用户规格 #3）。
* ``merge(task_id, real_pct)`` —— 真实进度可用时取 ``max(fake, real)``，
  真实进度追上后自然接管显示（用户规格「真实进度可用时以真实为准」）。
* ``finish(task_id)`` —— 任务完成时归 100%。
* ``stop(task_id)`` —— 任务失败 / 被移除时清理状态。

进度永远 ``clamp(0, 100)``，单调不回退、不循环（修复「涨满又归零」）。

预计时间估算（``estimate_seconds``）
------------------------------------
* 视频类任务：``task.duration_ms > 0`` 时取 ``duration_ms / 1000``；
  否则按 ``src_size`` 粗估（``5 MB/s`` 是 x264 slow 预设的典型吞吐）。
* 音频：``20 MB/s``。
* 图片：固定 3s（绝大多数秒级完成）。
* 压缩阶段：``2 MB/s``，至少 2s。
* :class:`~momentshift.core.task_pool.PoolItem` 没有 ``src_size``，按
  ``payload.src`` 现算文件大小。

线程模型
--------
``FakeProgressTracker`` 是纯逻辑（依赖可注入的 ``clock``），
``FakeProgressDriver`` 持有 ``QTimer``，必须在创建它的线程（GUI 线程）
使用。回调 ``set_lookup(fn)`` 把「按 task_id 取任务对象」的能力交给
调用方，避免驱动器耦合 :class:`~momentshift.core.models.Task` 或
:class:`~momentshift.core.task_pool.PoolItem` 的具体类型。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .qt_compat import QObject, QTimer, Signal

# 假进度条的关键常数。修改这两个值会改变所有任务的「假进度曲线」节奏，
# 因此暴露为模块级常量方便测试与回归对比。
START_PCT = 5.0
CAP_PCT = 85.0
DEFAULT_TICK_MS = 500  # GUI 线程定时器节拍；与现有 200/250ms 动效错开


def estimate_seconds(task: Any, phase: str = "convert") -> float:
    """为假进度条估算任务预计耗时（秒）。

    Args:
        task: 任意带 ``category`` / ``src_size`` / ``duration_ms`` 属性的对象，
            或 :class:`~momentshift.core.task_pool.PoolItem`（从 ``payload`` 取
            ``src`` 后现场算大小）。
        phase: ``"convert"`` / ``"compress"``，两阶段的估算策略不同。

    Returns:
        预计秒数；下限 0.5 防止「按 0 秒线性」瞬时跳到 85%。

    Notes:
        这些数字是**粗估**，仅用于驱动假进度曲线的斜率。曲线封顶 85%，
        真实进度回调会接管显示，所以偏差只是「假阶段涨得快 / 慢」，不会
        让用户看到错误的最终状态。
    """
    size = int(getattr(task, "src_size", 0) or 0)
    duration_ms = int(getattr(task, "duration_ms", 0) or 0)
    category = str(getattr(task, "category", "") or "")

    # PoolItem 走 payload.src；裸 dict 也兼容（测试用）。
    if not size:
        payload: Any = getattr(task, "payload", None)
        if isinstance(payload, dict):
            src = payload.get("src") or payload.get("input_path")
            if src:
                try:
                    size = Path(src).stat().st_size
                except OSError:
                    size = 0
        elif isinstance(task, dict):
            src = task.get("src") or task.get("input_path")
            if src:
                try:
                    size = Path(src).stat().st_size
                except OSError:
                    size = 0

    if phase == "compress":
        return max(2.0, size / (2 * 1024 * 1024)) if size else 5.0

    if duration_ms > 0:
        # 视频有真实时长：按「近似实时」估（x264 slow 约 0.3–1× 实时）。
        return max(3.0, duration_ms / 1000.0)
    if category == "video":
        return max(10.0, size / (5 * 1024 * 1024)) if size else 30.0
    if category == "audio":
        return max(3.0, size / (20 * 1024 * 1024)) if size else 10.0
    # 图片：典型秒级完成
    return 3.0


class FakeProgressTracker:
    """纯逻辑：每条任务的假进度状态机。不依赖 Qt，可离屏单测。

    典型用法::

        tracker = FakeProgressTracker(clock=fake_clock)
        tracker.start("t1", estimate_seconds=10.0)
        v1 = tracker.fake("t1")            # 立即 5
        v2 = tracker.fake("t1")            # 推进几秒后：5..85 线性
        merged = tracker.merge("t1", 50)   # max(fake, real)
        tracker.finish("t1")                # 归 100

    进度单调约束：
    * ``fake`` 单调递增（输入单调的 clock）；
    * ``merge`` 取 ``max(fake, real)``，真实进度单调时合并值也单调；
    * ``finish`` 写 100；
    * 任何返回都 ``clamp(0, 100)``，绝不会 >100 或 <0。
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._starts: dict[str, float] = {}
        self._estimates: dict[str, float] = {}
        self._last: dict[str, int] = {}
        # 已 finish 的任务：fake / merge 必须返回 100，防止驱动器在
        # finish 后被某个漏调的 merge 自动重启假进度条。
        self._finished: set[str] = set()

    # ----- 生命周期 -----
    def start(self, task_id: str, estimate_seconds: float) -> None:
        """开始计时并把进度锚定 :data:`START_PCT` (5%)。"""
        # 允许对已 finish 的任务重新 start（retry 路径）。
        self._finished.discard(task_id)
        self._starts[task_id] = self._clock()
        self._estimates[task_id] = max(0.5, float(estimate_seconds))
        self._last[task_id] = int(START_PCT)

    def stop(self, task_id: str) -> None:
        """任务失败 / 被移除 / 清理时调用。"""
        self._starts.pop(task_id, None)
        self._estimates.pop(task_id, None)
        self._last.pop(task_id, None)
        self._finished.discard(task_id)

    def finish(self, task_id: str) -> int:
        """任务完成，归 100 并标记终止。

        ``finish`` 是显式终止：清掉 start / estimate 让 ``fake`` 不再被
        计算；标记 ``_finished`` 让后续 ``fake`` / ``merge`` 直接返回 100，
        避免被自动重启。
        """
        self._finished.add(task_id)
        self._starts.pop(task_id, None)
        self._estimates.pop(task_id, None)
        self._last[task_id] = 100
        return 100

    # ----- 查询 -----
    def fake(self, task_id: str) -> int:
        """当前假进度（0..100）。

        已 ``finish`` 的任务固定返回 100；未开始返回 0；超过预计时间封顶
        :data:`CAP_PCT`。
        """
        if task_id in self._finished:
            return 100
        start = self._starts.get(task_id)
        est = self._estimates.get(task_id)
        if start is None or est is None:
            return 0
        elapsed = max(0.0, self._clock() - start)
        frac = min(1.0, elapsed / est)
        return int(START_PCT + frac * (CAP_PCT - START_PCT))

    def merge(self, task_id: str, real_pct: int) -> int:
        """合并真实进度：``max(fake, real, last)``，clamp 0..100。

        三层 ``max`` 的语义：

        * ``max(fake, real)`` —— 真实追上假进度后自然接管显示；
        * ``max(..., last)`` —— 即便真实进度偶发回退（迟到的旧值 / 网络抖动），
          显示值也**永不回退**，修复用户报告的「涨满又归零」。

        已 ``finish`` 的任务返回 100（合并值不会超过终态）。
        """
        if task_id in self._finished:
            self._last[task_id] = 100
            return 100
        real = max(0, min(100, int(real_pct)))
        candidate = max(self.fake(task_id), real)
        last_val = self._last.get(task_id, -1)
        if last_val < 0:
            # merge 早于 start（驱动器会兜底自动 start，此分支仅兜底纯逻辑调用）
            merged = candidate
        else:
            merged = max(last_val, candidate)
        self._last[task_id] = merged
        return merged

    def last(self, task_id: str) -> int:
        """最近一次记录的展示值（未记录返回 -1，供驱动器节流用）。"""
        return self._last.get(task_id, -1)

    def set_last(self, task_id: str, value: int) -> None:
        """让驱动器把「刚发出去的展示值」记下来，给下次节流用。"""
        self._last[task_id] = int(value)

    def active_ids(self) -> list[str]:
        """所有处于「已 start 未 stop/finish」的 task_id（用于驱动器扫描）。"""
        return [tid for tid in self._starts if tid not in self._finished]


# 任务查找回调：按 task_id 返回任务对象，找不到返回 ``None``。
# 驱动器用它来判断「任务是否还在运行中」与「通道归属」。
TaskLookup = Callable[[str], Any]


def _is_active(item: Any) -> bool:
    """泛型判定任务对象是否仍处于「运行中」。

    支持 :class:`~momentshift.core.models.Task`（``status`` 字段）和
    :class:`~momentshift.core.task_pool.PoolItem`（``state`` 字段）。
    """
    if item is None:
        return False
    status = getattr(item, "status", None)
    if isinstance(status, str):
        return status in ("running", "compressing")
    state = getattr(item, "state", None)
    if state is None:
        return True
    value = getattr(state, "value", state)
    return value in ("running", "compressing")


class FakeProgressDriver(QObject):
    """把 :class:`FakeProgressTracker` 装上 ``QTimer`` 的 GUI 线程驱动器。

    Signals:
        progress_changed(str, int): 转换阶段假进度，``(task_id, 0..100)``。
        compress_progress_changed(str, int): 压缩阶段假进度，``(task_id, 0..100)``。

    每个管理器（:class:`~momentshift.core.queue.ConversionManager` /
    :class:`~momentshift.core.task_pool.TaskPool`）持有一个实例即可。
    """

    progress_changed = Signal(str, int)
    compress_progress_changed = Signal(str, int)

    def __init__(
        self,
        parent: QObject | None = None,
        interval_ms: int = DEFAULT_TICK_MS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(parent)
        self._tracker = FakeProgressTracker(clock=clock)
        self._lookup: TaskLookup | None = None
        self._channels: dict[str, str] = {}  # task_id -> "convert" | "compress"
        self._emitted: dict[str, int] = {}  # task_id -> 上次发出的展示值
        self._timer = QTimer(self)
        self._timer.setInterval(max(50, int(interval_ms)))
        self._timer.timeout.connect(self._tick)

    # ----- 配置 -----
    def set_lookup(self, lookup: TaskLookup | None) -> None:
        """注入「按 task_id 取任务对象」的回调。"""
        self._lookup = lookup

    # ----- 生命周期（由管理器调用） -----
    def start(self, task_id: str, estimate_seconds: float, channel: str = "convert") -> None:
        """开始追踪某条任务的假进度，并立刻发出 START_PCT (5%)。"""
        self._tracker.start(task_id, estimate_seconds)
        self._channels[task_id] = channel
        self._emitted[task_id] = -1
        if not self._timer.isActive():
            self._timer.start()
        self._emit(task_id, self._tracker.fake(task_id))

    def merge(self, task_id: str, real_pct: int) -> int:
        """真实进度回调：合并后返回展示值，并按需发出信号。"""
        if task_id not in self._channels:
            # 未 start 直接 merge（极早的真实进度先到）：保守当作 0 起算。
            self._tracker.start(task_id, estimate_seconds(DEFAULT_TICK_MS / 1000.0 * 80.0))
            self._channels[task_id] = "convert"
            self._emitted[task_id] = -1
            if not self._timer.isActive():
                self._timer.start()
        v = self._tracker.merge(task_id, real_pct)
        self._emit(task_id, v)
        return v

    def finish(self, task_id: str) -> None:
        """任务完成：归 100 并发出（强制覆盖去重）。"""
        if task_id not in self._channels:
            return
        self._tracker.finish(task_id)
        self._emit(task_id, 100, force=True)
        self._cleanup(task_id)

    def stop(self, task_id: str) -> None:
        """任务失败 / 被移除 / 不再活跃：清理状态，不发信号。"""
        self._tracker.stop(task_id)
        self._cleanup(task_id)

    # ----- 内部 -----
    def _emit(self, task_id: str, pct: int, force: bool = False) -> None:
        """节流发出：与上次相等就不发；``force=True`` 跳过节流用于 finish。"""
        if not force and self._emitted.get(task_id, -1) == pct:
            return
        self._emitted[task_id] = pct
        channel = self._channels.get(task_id, "convert")
        if channel == "compress":
            self.compress_progress_changed.emit(task_id, pct)
        else:
            self.progress_changed.emit(task_id, pct)

    def _tick(self) -> None:
        """定时器回调：扫描活跃任务，按通道发出最新假进度。"""
        if self._lookup is None:
            return
        # 收集本轮要处理的 id，避免运行时修改 dict。
        ids = list(self._channels.keys())
        for task_id in ids:
            item = self._lookup(task_id)
            if item is None or not _is_active(item):
                # 任务没了或状态变了（finished/failed/canceled）→ 停追踪
                self._tracker.stop(task_id)
                self._cleanup(task_id)
                continue
            v = self._tracker.fake(task_id)
            self._emit(task_id, v)

    def _cleanup(self, task_id: str) -> None:
        """从所有记账容器里移除 task_id；空了就停定时器。"""
        self._channels.pop(task_id, None)
        self._emitted.pop(task_id, None)
        if not self._channels and self._timer.isActive():
            self._timer.stop()