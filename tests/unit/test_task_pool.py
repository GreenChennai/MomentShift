"""``core.task_pool.TaskPool`` 的单元测试。

按 v0.8.0 风险缓解 E1-R1 的要求，队列引擎的测试要**先于**界面迁移写出来，并且
不依赖 GUI：这里只用 ``QCoreApplication``（无窗口系统依赖）驱动事件循环，线程池
也注入独立实例，避免和全局池上的其它测试相互干扰。

覆盖重点是旧实现里靠「小心别踩」维持的那些边界：
- 任务跑到一半被清空 / 被移除（旧实现会 KeyError 并卡死并发坑位）
- 暂停期间不得再投递新任务，恢复后要能接上
- 并发上限必须真的封顶，且改设置能即时生效
- run_fn 抛异常不能让任务永远停在「运行中」
"""

from __future__ import annotations

import threading
import time

import pytest

from momentshift.core.qt_compat import QCoreApplication, QThreadPool
from momentshift.core.task_pool import PoolItem, TaskPool, TaskState


@pytest.fixture(scope="module")
def qapp():
    """整个模块共用一个 Qt 应用对象。

    Qt 不允许一个进程里存在两个 QCoreApplication，所以已有实例时直接复用
    （例如别的测试文件先建了 QApplication）。
    """
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


@pytest.fixture()
def pool_factory(qapp):
    """产出 TaskPool 的工厂，并在用例结束后确保线程池排空。"""
    created: list[tuple[TaskPool, QThreadPool]] = []

    def make(run_fn, max_workers=2, prepare_fn=None):
        threads = QThreadPool()
        # 线程池自身给足容量，真正的并发上限由 TaskPool 的调度循环封顶——
        # 这样测出来的才是 TaskPool 的行为，而不是 QThreadPool 的行为。
        threads.setMaxThreadCount(16)
        pool = TaskPool(
            run_fn,
            max_workers=max_workers,
            prepare_fn=prepare_fn,
            thread_pool=threads,
        )
        created.append((pool, threads))
        return pool

    yield make

    for _pool, threads in created:
        threads.waitForDone(5000)


def pump(app, predicate, timeout: float = 10.0) -> bool:
    """转事件循环直到 ``predicate()`` 为真或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.002)
    app.processEvents()
    return predicate()


def instant_ok(item: PoolItem, report, cancel) -> tuple[bool, str]:
    """立刻成功的业务函数。"""
    report(50)
    return True, "ok"


# =============================================================================
# 基础流程
# =============================================================================
def test_add_is_idempotent_by_iid(pool_factory):
    pool = pool_factory(instant_ok)
    assert pool.add("a", "A.png") is True
    assert pool.add("a", "A.png") is False
    assert len(pool) == 1
    assert pool.iids() == ["a"]


def test_run_all_to_done(qapp, pool_factory):
    pool = pool_factory(instant_ok)
    finished: list[tuple[str, str]] = []
    pool.itemFinished.connect(lambda iid, state, msg: finished.append((iid, state)))
    all_done: list[int] = []
    pool.allFinished.connect(lambda: all_done.append(1))

    for i in range(5):
        pool.add(f"f{i}", f"f{i}.png")
    pool.start()

    assert pump(qapp, lambda: len(finished) == 5), finished
    assert pump(qapp, lambda: bool(all_done))
    assert {state for _, state in finished} == {"done"}
    assert pool.is_running is False
    counts = pool.counts()
    assert counts["total"] == 5 and counts["done"] == 5
    assert counts["failed"] == 0 and counts["pending"] == 0


def test_start_without_items_is_noop(pool_factory):
    pool = pool_factory(instant_ok)
    pool.start()
    assert pool.is_running is False


def test_progress_is_relayed(qapp, pool_factory):
    def with_progress(item, report, cancel):
        for pct in (10, 40, 90):
            report(pct)
        return True, ""

    pool = pool_factory(with_progress)
    seen: list[int] = []
    pool.itemProgress.connect(lambda iid, pct: seen.append(pct))
    pool.add("a")
    pool.start()
    assert pump(qapp, lambda: pool.counts()["done"] == 1)
    # 0 是 worker 起步时自动补的一发
    assert seen[0] == 0
    assert 90 in seen
    assert pool.item("a").progress == 100


def test_progress_is_clamped(qapp, pool_factory):
    def wild(item, report, cancel):
        report(-30)
        report(250)
        return True, ""

    pool = pool_factory(wild)
    seen: list[int] = []
    pool.itemProgress.connect(lambda iid, pct: seen.append(pct))
    pool.add("a")
    pool.start()
    assert pump(qapp, lambda: pool.counts()["done"] == 1)
    assert min(seen) >= 0 and max(seen) <= 100


def test_result_dict_carries_business_payload(qapp, pool_factory):
    def fill_result(item, report, cancel):
        item.result["saved"] = 1234
        item.result["backend"] = "oxipng"
        return True, "done"

    pool = pool_factory(fill_result)
    pool.add("a")
    pool.start()
    assert pump(qapp, lambda: pool.counts()["done"] == 1)
    assert pool.item("a").result == {"saved": 1234, "backend": "oxipng"}


def test_payload_is_passed_through_untouched(qapp, pool_factory):
    marker = {"engine": "realesrgan", "scale": 4}
    captured: list[object] = []

    def grab(item, report, cancel):
        captured.append(item.payload)
        return True, ""

    pool = pool_factory(grab)
    pool.add("a", "A", payload=marker)
    pool.start()
    assert pump(qapp, lambda: pool.counts()["done"] == 1)
    assert captured == [marker]
    assert captured[0] is marker


# =============================================================================
# 失败 / 异常
# =============================================================================
def test_failure_marks_failed_and_keeps_message(qapp, pool_factory):
    pool = pool_factory(lambda item, report, cancel: (False, "boom"))
    pool.add("a")
    pool.start()
    assert pump(qapp, lambda: pool.counts()["failed"] == 1)
    item = pool.item("a")
    assert item.state is TaskState.FAILED
    assert item.message == "boom"


def test_exception_in_run_fn_does_not_wedge_the_queue(qapp, pool_factory):
    """业务函数抛异常时任务必须落到 failed，并发坑位必须释放。"""

    def explode(item, report, cancel):
        if item.iid == "bad":
            raise RuntimeError("intentional")
        return True, ""

    pool = pool_factory(explode, max_workers=1)
    pool.add("bad")
    pool.add("good")
    pool.start()

    assert pump(qapp, lambda: pool.counts()["done"] + pool.counts()["failed"] == 2)
    assert pool.item("bad").state is TaskState.FAILED
    assert pool.item("good").state is TaskState.DONE
    assert pool.is_running is False


def test_restart_reruns_failed_items_only(qapp, pool_factory):
    attempts: dict[str, int] = {}
    lock = threading.Lock()

    def flaky(item, report, cancel):
        with lock:
            attempts[item.iid] = attempts.get(item.iid, 0) + 1
            count = attempts[item.iid]
        if item.iid == "b" and count == 1:
            return False, "first try fails"
        return True, ""

    pool = pool_factory(flaky, max_workers=1)
    pool.add("a")
    pool.add("b")
    pool.start()
    assert pump(qapp, lambda: not pool.is_running and pool.counts()["failed"] == 1)

    pool.start()  # 再点一次「开始」＝重试所有未成功项
    assert pump(qapp, lambda: pool.counts()["done"] == 2)
    assert attempts == {"a": 1, "b": 2}


def test_retry_single_item(qapp, pool_factory):
    calls: list[str] = []

    def once(item, report, cancel):
        calls.append(item.iid)
        return len(calls) > 1, "x"

    pool = pool_factory(once, max_workers=1)
    pool.add("a")
    pool.start()
    assert pump(qapp, lambda: pool.counts()["failed"] == 1)

    assert pool.retry("a") is True
    pool.start()
    assert pump(qapp, lambda: pool.counts()["done"] == 1)
    assert pool.retry("missing") is False


# =============================================================================
# 并发上限
# =============================================================================
def _blocking_run_fn():
    """返回 (run_fn, gate, peak_getter)：run_fn 会卡在 gate 上直到被放行。"""
    gate = threading.Event()
    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    def run_fn(item, report, cancel):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        gate.wait(5.0)
        with lock:
            state["current"] -= 1
        return True, ""

    return run_fn, gate, lambda: state["peak"], lambda: state["current"]


def test_concurrency_never_exceeds_limit(qapp, pool_factory):
    run_fn, gate, peak, current = _blocking_run_fn()
    pool = pool_factory(run_fn, max_workers=3)
    for i in range(10):
        pool.add(f"f{i}")
    pool.start()

    assert pump(qapp, lambda: current() == 3, timeout=5.0)
    time.sleep(0.15)  # 给「多投递」留出暴露窗口
    qapp.processEvents()
    assert current() == 3, "并发数超出上限"

    gate.set()
    assert pump(qapp, lambda: pool.counts()["done"] == 10, timeout=15.0)
    assert peak() <= 3


def test_max_workers_can_be_a_callable(qapp, pool_factory):
    run_fn, gate, peak, current = _blocking_run_fn()
    limit = {"value": 1}
    pool = pool_factory(run_fn, max_workers=lambda: limit["value"])
    for i in range(6):
        pool.add(f"f{i}")
    pool.start()

    assert pump(qapp, lambda: current() == 1, timeout=5.0)
    time.sleep(0.1)
    qapp.processEvents()
    assert current() == 1

    limit["value"] = 4  # 模拟用户在设置页调大最大线程数
    gate.set()
    assert pump(qapp, lambda: pool.counts()["done"] == 6, timeout=15.0)
    assert peak() >= 1


def test_invalid_max_workers_falls_back_to_one(pool_factory):
    pool = pool_factory(instant_ok, max_workers="not-a-number")
    assert pool._limit() == 1
    pool.set_max_workers(0)
    assert pool._limit() == 1
    pool.set_max_workers(5)
    assert pool._limit() == 5


# =============================================================================
# 暂停 / 继续
# =============================================================================
def test_pause_blocks_new_launches_and_resume_continues(qapp, pool_factory):
    started: list[str] = []
    release = threading.Event()

    def run_fn(item, report, cancel):
        started.append(item.iid)
        release.wait(5.0)
        return True, ""

    pool = pool_factory(run_fn, max_workers=1)
    for i in range(4):
        pool.add(f"f{i}")
    pool.start()
    assert pump(qapp, lambda: len(started) == 1)

    pool.pause()
    assert pool.is_paused is True
    release.set()
    # 第一条跑完了，但暂停期间不许再投递新的
    assert pump(qapp, lambda: pool.counts()["done"] == 1)
    time.sleep(0.15)
    qapp.processEvents()
    assert len(started) == 1, "暂停期间仍在投递新任务"
    assert pool.is_running is True, "还有待跑任务时不应判定为收敛"

    pool.resume()
    assert pump(qapp, lambda: pool.counts()["done"] == 4, timeout=15.0)
    assert pool.is_running is False


def test_toggle_pause_round_trip(qapp, pool_factory):
    release = threading.Event()
    pool = pool_factory(
        lambda item, report, cancel: (release.wait(5.0), True)[1] and (True, ""),
        max_workers=1,
    )
    pool.add("a")
    pool.add("b")
    pool.start()
    pool.toggle_pause()
    assert pool.is_paused is True
    pool.toggle_pause()
    assert pool.is_paused is False
    release.set()
    assert pump(qapp, lambda: pool.counts()["done"] == 2, timeout=15.0)


def test_pause_before_start_is_noop(pool_factory):
    pool = pool_factory(instant_ok)
    pool.add("a")
    pool.pause()
    assert pool.is_paused is False


# =============================================================================
# 运行中清空 / 移除（旧实现的 KeyError 重灾区）
# =============================================================================
def test_clear_while_running_does_not_raise(qapp, pool_factory):
    release = threading.Event()

    def run_fn(item, report, cancel):
        release.wait(5.0)
        return True, ""

    pool = pool_factory(run_fn, max_workers=2)
    for i in range(6):
        pool.add(f"f{i}")
    pool.start()
    assert pump(qapp, lambda: pool.counts()["running"] == 2)

    pool.clear()
    assert len(pool) == 0
    assert pool.is_running is False

    release.set()
    # 迟到的 finished 打到已清空的队列上：不得抛异常，也不得复活条目
    assert pump(qapp, lambda: True, timeout=1.0)
    qapp.processEvents()
    assert len(pool) == 0
    assert pool.counts()["total"] == 0


def test_remove_running_item_frees_the_slot(qapp, pool_factory):
    started: list[str] = []
    release = threading.Event()

    def run_fn(item, report, cancel):
        started.append(item.iid)
        release.wait(5.0)
        return True, ""

    pool = pool_factory(run_fn, max_workers=1)
    for i in range(3):
        pool.add(f"f{i}")
    pool.start()
    assert pump(qapp, lambda: len(started) == 1)

    # 移除正在跑的那条：坑位当场释放，下一条应立即顶上
    assert pool.remove(started[0]) is True
    assert pump(qapp, lambda: len(started) == 2, timeout=5.0)
    assert "f0" not in pool

    release.set()
    assert pump(qapp, lambda: pool.counts()["done"] == 2, timeout=15.0)


def test_remove_pending_item(pool_factory):
    pool = pool_factory(instant_ok, max_workers=1)
    pool.add("a")
    pool.add("b")
    assert pool.remove("b") is True
    assert pool.remove("b") is False
    assert pool.iids() == ["a"]


def test_cancel_all_keeps_items_for_retry(qapp, pool_factory):
    seen_cancel: list[bool] = []
    entered = threading.Semaphore(0)
    release = threading.Event()

    def run_fn(item, report, cancel):
        entered.release()
        release.wait(5.0)
        seen_cancel.append(cancel.is_set())
        return True, ""

    pool = pool_factory(run_fn, max_workers=2)
    for i in range(5):
        pool.add(f"f{i}")
    pool.start()
    # 必须等业务函数「真的进去了」再取消。只等 counts()['running']==2 是不够的：
    # RUNNING 是投递前在本线程同步置位的，此刻工作线程可能还没进 run()，
    # 那样会走 worker 的「排队期间就被取消」短路分支，run_fn 一次都不执行。
    assert entered.acquire(timeout=5.0)
    assert entered.acquire(timeout=5.0)

    pool.cancel_all()
    release.set()
    assert pump(qapp, lambda: pool.counts()["canceled"] == 2, timeout=15.0)
    assert pool.is_running is False
    assert len(pool) == 5, "cancel_all 不该删除条目"
    assert seen_cancel == [True, True], "run_fn 应当能看到取消标志"

    # 被取消的条目可以再次开始
    pool.start()
    assert pump(qapp, lambda: pool.counts()["done"] == 5, timeout=15.0)


def test_cancel_before_start_short_circuits(qapp, pool_factory):
    """排队期间就被取消的任务，业务函数一次都不该被调用。"""
    calls: list[str] = []
    release = threading.Event()

    def run_fn(item, report, cancel):
        calls.append(item.iid)
        release.wait(5.0)
        return True, ""

    pool = pool_factory(run_fn, max_workers=1)
    pool.add("a")
    pool.add("b")
    pool.start()
    assert pump(qapp, lambda: calls == ["a"])

    pool.cancel_all()
    release.set()
    assert pump(qapp, lambda: not pool.is_running, timeout=15.0)
    qapp.processEvents()
    assert calls == ["a"], "已取消的排队任务不该再被执行"


# =============================================================================
# prepare_fn 钩子
# =============================================================================
def test_prepare_fn_runs_in_caller_thread_and_serially(qapp, pool_factory):
    main_thread = threading.current_thread().ident
    threads_seen: list[int] = []
    order: list[str] = []

    def prepare(item):
        threads_seen.append(threading.current_thread().ident)
        order.append(item.iid)
        item.payload = {"out": f"{item.iid}.out"}
        return True

    captured: list[object] = []

    def run_fn(item, report, cancel):
        captured.append(item.payload)
        return True, ""

    pool = pool_factory(run_fn, max_workers=4, prepare_fn=prepare)
    for i in range(4):
        pool.add(f"f{i}")
    pool.start()
    assert pump(qapp, lambda: pool.counts()["done"] == 4)

    assert set(threads_seen) == {main_thread}, "prepare_fn 必须在调用方线程串行执行"
    assert order == ["f0", "f1", "f2", "f3"]
    assert {p["out"] for p in captured} == {f"f{i}.out" for i in range(4)}


def test_prepare_fn_veto_marks_failed_without_consuming_slot(qapp, pool_factory):
    ran: list[str] = []

    def prepare(item):
        return item.iid != "bad"

    def run_fn(item, report, cancel):
        ran.append(item.iid)
        return True, ""

    pool = pool_factory(run_fn, max_workers=1, prepare_fn=prepare)
    pool.add("bad")
    pool.add("good")
    pool.start()
    assert pump(qapp, lambda: pool.counts()["done"] == 1)
    assert ran == ["good"]
    assert pool.item("bad").state is TaskState.FAILED
    assert pool.is_running is False


# =============================================================================
# 统计 / 耗时 / 引用池
# =============================================================================
def test_counts_covers_every_state(pool_factory):
    pool = pool_factory(instant_ok)
    for i in range(3):
        pool.add(f"f{i}")
    counts = pool.counts()
    assert counts["total"] == 3
    for state in TaskState:
        assert state.value in counts
    assert counts["pending"] == 3


def test_elapsed_ms_freezes_after_finish(qapp, pool_factory):
    def slow(item, report, cancel):
        time.sleep(0.05)
        return True, ""

    pool = pool_factory(slow, max_workers=1)
    pool.add("a")
    pool.start()
    assert pump(qapp, lambda: not pool.is_running, timeout=15.0)
    frozen = pool.elapsed_ms()
    assert frozen >= 40
    time.sleep(0.05)
    assert pool.elapsed_ms() == frozen, "收敛后耗时不应继续增长"


def test_retired_pool_is_drained_without_timers(qapp, pool_factory):
    """ODD-14：worker 引用靠调度事件确定性释放，不依赖任何定时器。"""
    pool = pool_factory(instant_ok, max_workers=2)
    for i in range(8):
        pool.add(f"f{i}")
    pool.start()
    assert pump(qapp, lambda: pool.counts()["done"] == 8)
    assert pool._workers == {}, "运行期引用未释放"
    assert pool._retired == [], "退休引用池未清空"


def test_shared_signal_object_is_parented(pool_factory):
    """v0.7.19 / v0.7.24：信号对象必须挂 parent，且全池只有一个。"""
    pool = pool_factory(instant_ok)
    assert pool._signals.parent() is pool
    children = [c for c in pool.children() if type(c) is type(pool._signals)]
    assert len(children) == 1


# =============================================================================
# 压力回归（对应验收标准②）
# =============================================================================
def test_stress_mixed_operations_on_fifty_tasks(qapp, pool_factory):
    """50 个任务 + 开始/暂停/继续/移除/清空/重试组合，最终必须收敛且不崩。"""

    def run_fn(item, report, cancel):
        report(50)
        # 制造一批失败项，好让「再点开始＝重试」这条路径也被走到
        return (not item.iid.endswith("7")), "s"

    pool = pool_factory(run_fn, max_workers=4)
    for i in range(50):
        pool.add(f"t{i:02d}", f"t{i}.png")

    pool.start()
    pool.pause()
    pool.resume()
    pool.remove("t03")
    pool.remove("t11")
    assert pump(qapp, lambda: not pool.is_running, timeout=30.0)

    counts = pool.counts()
    assert counts["total"] == 48
    assert counts["running"] == 0 and counts["pending"] == 0
    assert counts["failed"] == 5  # t07 t17 t27 t37 t47
    assert counts["done"] == 43

    pool.start()  # 重试全部失败项
    assert pump(qapp, lambda: not pool.is_running, timeout=30.0)
    assert pool.counts()["failed"] == 5  # 仍然稳定失败，不会串状态

    pool.clear()
    assert len(pool) == 0
    assert pool.is_running is False
    assert pool.counts()["total"] == 0
