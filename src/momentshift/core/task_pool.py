"""通用队列引擎 —— 压缩 / 放大两个界面共用的并发调度核心。

职责边界：
- 做：并发上限控制、排队与派发、暂停/继续/移除/清空、进度与结束信号聚合。
- 不做：不关心任务具体干什么（由调用方传入 run_fn）；不碰界面控件。

依赖：core/logger、core/qt_compat；被依赖：gui/compress_interface、gui/upscale_interface。

为什么要有这个模块（DUP-01 / INFRA-06）
------------------------------------------------
``gui/compress_interface.py`` 与 ``gui/upscale_interface.py`` 各自维护了一套
逐行同构的队列管理器：``_pending`` / ``_active`` / ``_workers`` 三个容器、
``_on_start`` / ``_launch_next`` / ``_on_finished`` / ``_on_pause`` /
``_on_clear`` / ``_on_remove`` / ``_update_controls`` 七个方法，合计约 600 行。
两份代码只在「worker 干什么活」上不同，调度部分连变量名都一样。

代价是真实发生过的：v0.7.30 给压缩加 gifsicle 后端时只改了压缩这一侧，放大
那侧的同类问题（ODD-16）留到本次重构才发现。凡是「改一处必须记得改另一处」
的结构，迟早会漏改一次。

线程模型
--------
- 池对象本身活在创建它的线程（实际上是 GUI 线程）。所有内部容器
  （``_items`` / ``_pending`` / ``_active`` / ``_workers``）**只在该线程读写**。
- 真正干活的 ``run_fn`` 在 ``QThreadPool`` 的工作线程里执行。它与池之间只通过
  两条 Qt 信号通信（``progress`` / ``finished``），跨线程自动走队列连接，回到
  池所在线程再改状态。因此内部容器不需要加锁。
- ``run_fn`` 唯一可以在工作线程里写的东西是自己那条 :class:`PoolItem` 的
  ``result`` 字典；GUI 侧必须等 ``itemFinished`` 到达后才读它。

对象生命周期约定（v0.7.19 / v0.7.24 的血泪教训，别动）
--------------------------------------------------------
1. ``WorkerSignals`` **必须**有 parent。历史上它是 worker 的普通属性，worker
   跑完被回收时信号对象跟着没了，正在投递的队列连接打到野指针上，v0.7.19 和
   v0.7.24 各崩过一次。这里更进一步：整个池只用**一个**共享的信号对象，
   parent 就是池自己，随池同生共死；每条信号都带 ``iid`` 区分来源。顺带解决
   了旧写法「每个任务 new 一个 QObject 挂在界面上、永不释放」的堆积问题。
2. ``QRunnable`` 侧用 ``_retired`` 显式引用池做延迟释放，**不用**
   ``QTimer.singleShot(1500)``（ODD-14）。理由见 :meth:`TaskPool._drain_retired`。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .logger import get_logger
from .qt_compat import QObject, QRunnable, QThreadPool, Signal

log = get_logger("task_pool")

__all__ = ["PoolItem", "TaskPool", "TaskState"]


class TaskState(str, Enum):
    """队列条目状态。

    继承 ``str`` 是为了让 ``state.value`` 能直接塞进 Qt 信号、写进日志、和
    界面里既有的 ``"pending"`` / ``"running"`` / ``"done"`` / ``"failed"``
    字符串对齐——迁移期两边混用也不会出错。
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


# 进度回调：``run_fn`` 用它上报 0~100 的百分比。
ProgressCb = Callable[[int], None]

# 业务执行体。签名 ``(item, progress_cb, cancel_event) -> (ok, message)``。
# ``cancel_event`` 被置位表示用户已清空/移除该任务，实现方应尽快收尾并清理临时文件。
RunFn = Callable[["PoolItem", ProgressCb, threading.Event], "tuple[bool, str]"]

# 启动前钩子。在池所在线程（GUI 线程）串行调用，返回 False 表示放弃该任务。
PrepareFn = Callable[["PoolItem"], bool]


@dataclass
class PoolItem:
    """队列里的一条任务。

    ``payload`` 由调用方自定义（压缩配置 / 放大配置），池本身完全不解释它。
    ``result`` 留给 ``run_fn`` 回填额外产物（例如压缩省下的字节数、实际生效的
    后端名），因为 Qt 信号只能带回 ``(ok, message)`` 两个值，塞不下业务细节。
    """

    iid: str
    payload: Any = None
    display_name: str = ""
    state: TaskState = TaskState.PENDING
    progress: int = 0
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)

    @property
    def is_finished(self) -> bool:
        """是否已经跑完（成功、失败、被取消都算）。"""
        return self.state in (
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.CANCELED,
        )

    @property
    def is_restartable(self) -> bool:
        """能否被 :meth:`TaskPool.start` 重新收进待跑列表。"""
        return self.state in (
            TaskState.PENDING,
            TaskState.FAILED,
            TaskState.CANCELED,
        )


class _WorkerSignals(QObject):
    """池内共享的信号载体。

    两条信号都以 ``iid`` 打头，因为整个池共用一个实例（见模块文档「对象生命
    周期约定」第 1 条），接收端靠 ``iid`` 分辨是哪条任务发来的。
    """

    progress = Signal(str, int)  # (iid, 0~100)
    finished = Signal(str, bool, str)  # (iid, ok, message)


class _PoolWorker(QRunnable):
    """把一条 :class:`PoolItem` 丢进线程池执行的壳。

    自己不含任何业务逻辑，只负责：兜住异常、把进度和结果转成信号。
    """

    def __init__(
        self,
        item: PoolItem,
        run_fn: RunFn,
        cancel: threading.Event,
        signals: _WorkerSignals,
    ) -> None:
        super().__init__()
        # autoDelete=True 时 PyQt 会把 C++ 所有权交给 QThreadPool，run() 返回后由
        # Qt 侧销毁；Python 这边的引用只是为了别让包装对象在运行期被 GC。
        self.setAutoDelete(True)
        self.item = item
        self._run_fn = run_fn
        self._cancel = cancel
        self._signals = signals

    def run(self) -> None:
        """在工作线程中执行业务函数。

        注意 ``finished`` 必须是每条返回路径上的**最后一句**：池收到它之后会
        立刻回收 worker 引用，之后再碰 ``self`` 就是在和 Qt 的销毁赛跑。
        """
        iid = self.item.iid
        if self._cancel.is_set():
            # 排队期间就被清空/移除了，一行活都不用干。
            self._signals.finished.emit(iid, False, "canceled before start")
            return

        def report(pct: int) -> None:
            self._signals.progress.emit(iid, max(0, min(100, int(pct))))

        report(0)
        try:
            ok, message = self._run_fn(self.item, report, self._cancel)
        except Exception:
            # 业务函数抛异常不能让线程池吞掉：吞掉的话这条任务永远停在「运行中」，
            # 队列也永远不会收敛（_active 里的坑位不会释放）。
            log.exception("[task_pool] task %s raised", iid)
            ok, message = False, "exception (see log)"
        self._signals.finished.emit(iid, bool(ok), str(message or ""))


class TaskPool(QObject):
    """自管理的 QRunnable 队列引擎。

    界面只负责三件事：往池里 :meth:`add` 任务、提供干活的 ``run_fn``、把池发出
    的信号渲染成列表行。调度、并发上限、暂停/继续/取消、统计、worker 生命周期
    全部收在这里。

    :param run_fn: 业务执行体，见 :data:`RunFn`。
    :param max_workers: 并发上限。可以传一个 ``int``，也可以传无参可调用对象
        —— 后者用于「用户在设置页改了最大线程数，下一批任务立即生效」的场景，
        旧代码里就是每次循环都重读 ``cfg.maxThreads`` 实现的。
    :param prepare_fn: 可选的启动前钩子，在本线程串行调用。之所以要有它：输出
        路径是靠「探测文件是否已存在、不存在才占用」来去重的，必须串行执行，
        丢进工作线程并发跑会给两条任务分配到同一个文件名。
    :param thread_pool: 可选的线程池。默认用 ``QThreadPool.globalInstance()``，
        与 v0.7.30 行为一致；测试里可以注入独立实例避免相互干扰。
    """

    itemAdded = Signal(str, str)  # (iid, display_name)
    itemStarted = Signal(str)  # (iid)
    itemProgress = Signal(str, int)  # (iid, 0~100)
    itemFinished = Signal(str, str, str)  # (iid, TaskState.value, message)
    stateChanged = Signal()  # 运行/暂停/空闲或条目集合发生变化
    allFinished = Signal()  # 整队收敛（从「有活干」变成「没活干」）

    def __init__(
        self,
        run_fn: RunFn,
        max_workers: int | Callable[[], int] = 3,
        parent: QObject | None = None,
        prepare_fn: PrepareFn | None = None,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__(parent)
        self._run_fn = run_fn
        self._prepare_fn = prepare_fn
        self._max_workers = max_workers
        self._pool = thread_pool if thread_pool is not None else QThreadPool.globalInstance()

        self._items: dict[str, PoolItem] = {}
        self._order: list[str] = []  # 插入顺序，决定调度顺序
        self._pending: list[str] = []
        self._active: set[str] = set()
        self._workers: dict[str, _PoolWorker] = {}
        self._retired: list[_PoolWorker] = []
        self._cancels: dict[str, threading.Event] = {}

        self._running = False
        self._paused = False
        self._started_at = 0.0
        self._elapsed_ms = 0

        # parent=self：随池同生共死，杜绝  /  那种「信号对象先没了」
        # 的崩溃。跨线程发射走队列连接，槽函数回到池所在线程执行。
        self._signals = _WorkerSignals(self)
        self._signals.progress.connect(self._on_worker_progress)
        self._signals.finished.connect(self._on_worker_finished)

    # =========================================================================
    # 队列操作
    # =========================================================================

    def add(self, iid: str, display_name: str = "", payload: Any = None) -> bool:
        """加入一条任务。``iid`` 重复时返回 ``False`` 且不做任何改动。

        沿用旧界面的语义：``iid`` 就是源文件路径，同一个文件不重复入队。
        """
        if iid in self._items:
            return False
        item = PoolItem(iid=iid, payload=payload, display_name=display_name or iid)
        self._items[iid] = item
        self._order.append(iid)
        self.itemAdded.emit(iid, item.display_name)
        self.stateChanged.emit()
        return True

    def remove(self, iid: str) -> bool:
        """移除一条任务。正在跑的会收到取消信号，但不强杀。"""
        item = self._items.pop(iid, None)
        if item is None:
            return False
        if iid in self._order:
            self._order.remove(iid)
        if iid in self._pending:
            self._pending.remove(iid)
        event = self._cancels.get(iid)
        if event is not None:
            event.set()
        # 立刻让出并发坑位：这条任务的结果已经没人要了，不该再占着不放。
        self._active.discard(iid)
        self.stateChanged.emit()
        if self._running and not self._paused:
            self._launch_next()
        else:
            self._settle()
        return True

    def clear(self) -> None:
        """清空队列并停止调度。在跑的任务收到取消信号后自行收尾。"""
        for event in self._cancels.values():
            event.set()
        self._items.clear()
        self._order.clear()
        self._pending.clear()
        self._active.clear()
        if self._running:
            self._elapsed_ms = self._compute_elapsed()
        self._running = False
        self._paused = False
        self._started_at = 0.0
        self.stateChanged.emit()

    def retry(self, iid: str) -> bool:
        """把一条已结束的任务打回待跑。运行中的任务不允许重试。"""
        item = self._items.get(iid)
        if item is None or item.state is TaskState.RUNNING:
            return False
        item.state = TaskState.PENDING
        item.progress = 0
        item.message = ""
        item.result.clear()
        self._cancels.pop(iid, None)
        if iid not in self._pending:
            self._pending.append(iid)
        self.stateChanged.emit()
        if self._running and not self._paused:
            self._launch_next()
        return True

    # =========================================================================
    # 运行控制
    # =========================================================================

    def start(self) -> None:
        """开始跑队列。

        与 v0.7.30 一致：每次调用都**重新收集**所有还没成功的条目（待处理 /
        失败 / 被取消），因此「跑完一轮 → 再点开始」等价于重试所有失败项。
        已经 done 的不会重跑，正在 running 的不会被重复投递。
        """
        queued = [iid for iid in self._order if self._items[iid].is_restartable]
        if not queued:
            return
        for iid in queued:
            item = self._items[iid]
            item.state = TaskState.PENDING
            item.progress = 0
            item.message = ""
            self._cancels.pop(iid, None)
        self._pending = queued
        if not self._running:
            self._started_at = time.monotonic()
            self._elapsed_ms = 0
        self._running = True
        self._paused = False
        self._launch_next()

    def pause(self) -> None:
        """暂停调度。已经在跑的任务跑完为止，不会被打断。"""
        if not self._running or self._paused:
            return
        self._paused = True
        self.stateChanged.emit()

    def resume(self) -> None:
        """从暂停恢复。"""
        if not self._paused:
            return
        self._paused = False
        self.stateChanged.emit()
        if self._running:
            self._launch_next()

    def toggle_pause(self) -> None:
        """暂停 / 继续二合一，对应界面上那颗会变字的按钮。"""
        if self._running and not self._paused:
            self.pause()
        else:
            self.resume()

    def cancel_all(self) -> None:
        """停止调度并请求所有在跑任务尽快退出，但**保留**队列条目便于重试。

        与 :meth:`clear` 的区别：clear 是「这些任务我不要了」，cancel_all 是
        「先停下，条目还留着」。
        """
        self._pending.clear()
        for iid in list(self._active):
            event = self._cancels.get(iid)
            if event is not None:
                event.set()
        if self._running:
            self._elapsed_ms = self._compute_elapsed()
        self._running = False
        self._paused = False
        self.stateChanged.emit()
        self._settle()

    # =========================================================================
    # 内部调度
    # =========================================================================

    def _limit(self) -> int:
        """当前并发上限。每次调度都重算，好让设置页的改动即时生效。"""
        raw = self._max_workers() if callable(self._max_workers) else self._max_workers
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            log.warning("[task_pool] 并发上限取值异常：%r，回退为 1", raw)
            return 1

    def set_max_workers(self, max_workers: int | Callable[[], int]) -> None:
        """替换并发上限（值或取值函数）。"""
        self._max_workers = max_workers

    def _launch_next(self) -> None:
        """把空闲坑位填满。可重入安全：只在池所在线程被调用。"""
        self._drain_retired()
        while (
            self._running
            and not self._paused
            and len(self._active) < self._limit()
            and self._pending
        ):
            iid = self._pending.pop(0)
            item = self._items.get(iid)
            if item is None:
                # 出队前被 remove 掉了，跳过即可。
                continue
            if self._prepare_fn is not None and not self._prepare_fn(item):
                # 钩子拒绝（例如输出目录创建失败），直接判失败，不占坑位。
                item.state = TaskState.FAILED
                item.message = item.message or "prepare failed"
                self.itemFinished.emit(iid, item.state.value, item.message)
                continue
            cancel = threading.Event()
            self._cancels[iid] = cancel
            item.state = TaskState.RUNNING
            item.progress = 0
            self._active.add(iid)
            worker = _PoolWorker(item, self._run_fn, cancel, self._signals)
            self._workers[iid] = worker
            # 先发 itemStarted 再投递，保证界面把行刷成「运行中」早于任何进度回调。
            self.itemStarted.emit(iid)
            self._pool.start(worker)
        self.stateChanged.emit()
        self._settle()

    def _on_worker_progress(self, iid: str, pct: int) -> None:
        item = self._items.get(iid)
        if item is None or item.state is not TaskState.RUNNING:
            # 任务已被移除或已收尾，迟到的进度直接丢弃，别把完成态刷回去。
            return
        item.progress = pct
        self.itemProgress.emit(iid, pct)

    def _on_worker_finished(self, iid: str, ok: bool, message: str) -> None:
        """worker 收工。本槽运行在池所在线程。

        必须容忍 ``iid`` 已经不在队列里：用户完全可以在任务跑到一半时点「清空」
        或删掉那一行。旧实现在这里是 ``self._items[item_id]["status"] = ...``
        的直接下标写入，那种情况下会抛 KeyError 并且让 ``_active`` 里的坑位再也
        释放不掉，整个队列就此卡死。
        """
        worker = self._workers.pop(iid, None)
        if worker is not None:
            self._retired.append(worker)
        self._active.discard(iid)
        cancel = self._cancels.pop(iid, None)

        item = self._items.get(iid)
        if item is None:
            self._settle()
            self.stateChanged.emit()
            return

        if cancel is not None and cancel.is_set():
            state = TaskState.CANCELED
        else:
            state = TaskState.DONE if ok else TaskState.FAILED
        item.state = state
        item.message = message
        if state is TaskState.DONE:
            item.progress = 100
        self.itemFinished.emit(iid, state.value, message)

        if self._running and not self._paused:
            self._launch_next()  # 内部已含 _settle
        else:
            self._settle()
        self.stateChanged.emit()

    def _settle(self) -> None:
        """检查整队是否已经收敛，收敛则落定耗时并发 ``allFinished``。"""
        if self._pending or self._active:
            return
        if not self._running:
            self._drain_retired()
            return
        self._running = False
        self._elapsed_ms = self._compute_elapsed()
        self._drain_retired()
        self.allFinished.emit()

    def _drain_retired(self) -> None:
        """释放已完成 worker 的 Python 引用。

        为什么不照抄 ``core/queue.py`` 里的 ``QTimer.singleShot(1500, ...)``：
        那 1500ms 是个拍脑袋的数——队列跑得快时延时还没到就又攒一批，跑得慢时
        白白多占引用；更要命的是它把「引用该活多久」这件事和真实的执行进度脱钩，
        出问题根本没法复现。

        这里改成事件驱动：引用先在 ``finished`` 槽里挪进 ``_retired``，真正清空
        发生在**下一次**调度或整队收尾时。此刻对应的 ``run()`` 早已返回（能进到
        下一轮调度，说明 finished 事件已经处理完了），既不存在提前回收的窗口，
        也不依赖任何时间常数。
        """
        if self._retired:
            self._retired.clear()

    def _compute_elapsed(self) -> int:
        if not self._started_at:
            return self._elapsed_ms
        return int((time.monotonic() - self._started_at) * 1000)

    # =========================================================================
    # 只读状态
    # =========================================================================

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_busy(self) -> bool:
        """是否处于「运行中且没暂停」——界面用它决定开始按钮要不要变灰。"""
        return self._running and not self._paused

    def item(self, iid: str) -> PoolItem | None:
        return self._items.get(iid)

    def iids(self) -> list[str]:
        """按入队顺序返回所有任务 id。"""
        return list(self._order)

    def items(self) -> list[PoolItem]:
        """按入队顺序返回所有任务。"""
        return [self._items[iid] for iid in self._order]

    def counts(self) -> dict[str, int]:
        """各状态计数，键为 ``TaskState`` 的值，外加 ``total``。"""
        result = {"total": len(self._items)}
        for state in TaskState:
            result[state.value] = 0
        for item in self._items.values():
            result[item.state.value] += 1
        return result

    def elapsed_ms(self) -> int:
        """本轮运行耗时（毫秒）。运行中返回实时值，结束后返回定格值。"""
        if self._running and self._started_at:
            return self._compute_elapsed()
        return self._elapsed_ms

    def __contains__(self, iid: object) -> bool:
        return iid in self._items

    def __len__(self) -> int:
        return len(self._items)
