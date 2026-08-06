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
from functools import partial
from pathlib import Path

from . import advanced, proc_control
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


def compress_after_conversion(
    task: Task, on_progress=None, on_stats=None, cancel_event=None, on_proc=None
) -> None:
    """格式转换完成后按需再跑一遍压缩，构成「转换 → 压缩」两步管线。

    Args:
        task: 已完成转换的任务。``compress_enabled`` 为假时直接返回。
        on_progress: 0..100 的压缩进度回调（V0.8.21 新增）。
        on_stats: :class:`~core.ffmpeg_progress.ProgressSnapshot` 回调，
            用于展示编码速度与预计剩余时间（V0.8.21 新增）。
        cancel_event: 置位后中止压缩（V0.8.21 新增）。此前压缩阶段的
            cancel 链是断的——``ffmpeg_compress.run`` 有这个形参，但调用处
            从来没传，导致压缩一旦开跑就只能等它自己结束。
        on_proc: 子进程句柄回调，供队列做 psutil 真暂停（V0.8.21 新增）。
    Notes:
        **V0.8.21 起支持三类媒体。** 此前这里硬编码 ``task.category != "image"``
        就直接 return，视频与音频的「转换后压缩」压根没通——高级设置里存的
        压缩参数存了也白存。

        - image：走 compressor 的后端路由
          （png→oxipng / jpg→jpegoptim / gif→gifsicle / 其他→pillow）。
        - video / audio：走 :func:`core.ffmpeg_compress.run`，参数取
          ``adv["compress"]`` 里的 ``ff_v_*`` / ``ff_a_*``。

        只在失败路径记日志——逐节点 info 在批量任务下会把日志文件淹没。
        压缩结果没变小时保留转换产物，仍记为压缩完成（节省量按 0 计）。
    """
    # compressor / ffmpeg_compress 只在这个分支用得上，模块级导入会让 core.queue
    # 无条件拖起 Pillow 与外部工具探测，故保持函数内延迟导入。
    from . import compressor

    if not task.compress_enabled:
        return
    if task.category not in ("image", "video", "audio"):
        return

    adv = task.adv or {}
    comp = adv.get("compress", {})
    if not isinstance(comp, dict) or not comp:
        return

    out = Path(task.output_path)
    if not out.is_file():
        log.warning("转换产物不存在，跳过压缩：%s", out.name)
        return

    fmt = out.suffix.lower().lstrip(".")

    # v0.8.22：图片「压缩程序」可选 FFmpeg 后端，先把后端定下来再决定 tmp 拼法。
    # ffmpeg 靠扩展名决定封装格式，临时文件必须保住原后缀；图片后端（oxipng /
    # jpegoptim / gifsicle / pillow）读 fmt 参数，不依赖 tmp 后缀，统一处理即可。
    ffmpeg_backend = False
    if task.category == "image":
        backend = comp.get("backend") or "auto"
        if backend == "auto":
            backend = compressor.default_backend(fmt)
        ffmpeg_backend = backend == "ffmpeg"

    # RISK-10：临时后缀复用 compressor 的常量，保证 cleanup_temp_files 能兜底认出它。
    tmp = str(task.output_path) + compressor.TMP_SUFFIX_COMPRESS
    if task.category in ("video", "audio") or ffmpeg_backend:
        # ffmpeg 靠扩展名决定封装格式，临时文件必须保住原后缀。
        # 直接拼成 "a.mp4.mstmp" 会让 ffmpeg 认不出容器，开局就报错。
        tmp = f"{out.with_suffix('')}{compressor.TMP_SUFFIX_COMPRESS}{out.suffix}"

    try:
        if task.category == "image":
            quality = int(comp.get("quality", 95))
            ok = compressor.compress(
                task.output_path, tmp, fmt, quality, backend=backend, opts=dict(comp)
            )
        else:
            from . import ffmpeg_compress

            ok, detail = ffmpeg_compress.run(
                task.output_path,
                tmp,
                opts=dict(comp),
                kind=task.category,
                on_progress=on_progress,
                on_stats=on_stats,
                cancel_event=cancel_event,
                on_proc=on_proc,
            )
            if not ok and detail and detail != "canceled":
                log.warning("FFmpeg 压缩失败（保留转换产物）：%s —— %s", out.name, detail)

        if ok and Path(tmp).exists():
            new_size = Path(tmp).stat().st_size
            old_size = task.dst_size or out.stat().st_size
            if 0 < new_size < old_size:
                Path(tmp).replace(task.output_path)
                task.dst_size = new_size
            else:
                # 压缩没能变小：保留转换结果，仍记为压缩完成（节省 0）
                task.dst_size = old_size
            task.compress_done = True
        else:
            log.warning("压缩未产出结果，保留原文件：%s", out.name)
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
    - ``stats(str, object)`` —— 编码速度 / 剩余时间等实时统计，参数为
      ``(任务 id, ProgressSnapshot)``（V0.8.21 新增）。

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
    # 用 object 而不是自定义类型：Signal 声明期不想为一个 dataclass 去注册
    # Qt 元类型，object 直接透传 Python 对象，跨线程队列连接照样安全。
    stats = Signal(str, object)

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
        self._owner = owner
        self.signals = WorkerSignals(owner)  # parent=manager

    def run(self) -> None:
        try:
            self.signals.started.emit(self.task.id)

            def on_progress(pct: int) -> None:
                self.signals.progress.emit(self.task.id, pct)

            def on_log(line: str) -> None:
                get_logger("ffmpeg").info("%s", line)

            def on_stats(snap) -> None:
                self.signals.stats.emit(self.task.id, snap)

            def on_proc(proc) -> None:
                # 直接回调 manager（同为 Python 对象，不经 Qt 信号）：
                # 暂停要立刻生效，走队列连接会被 GUI 事件循环延迟。
                # 注册表本身有锁，跨线程写入安全。
                if self._owner is not None:
                    self._owner.register_proc(self.task.id, proc)

            returncode, err = run_conversion(
                self.task,
                self.ffmpeg_path,
                self.hw,
                on_progress=on_progress,
                on_log=on_log,
                cancel_event=self.cancel_event,
                on_stats=on_stats,
                on_proc=on_proc,
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

    def __init__(self, task: Task, owner: QObject = None, cancel_event=None):
        super().__init__()
        self.setAutoDelete(True)
        self.task = task
        self._owner = owner
        self.cancel_event = cancel_event
        self.signals = WorkerSignals(owner)  # parent=manager

    def run(self) -> None:
        try:
            self.signals.compress_started.emit(self.task.id)
            self.task.pre_compress_size = self.task.dst_size

            # V0.8.21：压缩阶段接上真实进度。此前完全没传回调，视频压缩
            # 几分钟里进度条只能靠假进度瞎爬。
            def on_progress(pct: int) -> None:
                self.task.compress_progress = pct
                self.signals.compress_progress.emit(self.task.id, pct)

            def on_stats(snap) -> None:
                self.signals.stats.emit(self.task.id, snap)

            def on_proc(proc) -> None:
                if self._owner is not None:
                    self._owner.register_proc(self.task.id, proc)

            compress_after_conversion(
                self.task,
                on_progress=on_progress,
                on_stats=on_stats,
                cancel_event=self.cancel_event,
                on_proc=on_proc,
            )
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
    - ``task_stats(str, object)`` —— 实时编码统计（速度 / 剩余时间 / 帧率），
      参数为 ``(任务 id, ProgressSnapshot)``（V0.8.21 新增）。
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
    task_stats = Signal(str, object)
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
        self._retired: list[QRunnable] = []
        # 压缩阶段 worker 同理要防 GC。原实现 ``self._compress_pool.start(cw)``
        # 之后就把 cw 丢给局部变量，之所以没炸是因为 signals 的 parent 挂在
        # manager 上侥幸存活；v0.8.21 起压缩 worker 也发进度/统计信号，
        # 生命周期变长，这里显式持有到 _on_compress_finished 再释放。
        self._compress_workers: dict[str, CompressWorker] = {}
        # V0.8.21 E4：正在跑的 ffmpeg 子进程句柄，用于「真暂停」。
        # 写入方是 worker 线程（converter/ffmpeg_compress 的 on_proc 回调），
        # 读取方是 GUI 线程（pause/resume），必须上锁。
        self._procs: dict[str, object] = {}
        self._procs_lock = threading.Lock()
        # 压缩阶段的取消事件。此前压缩根本不可取消（run() 的 cancel_event
        # 形参一直是 None），暂停 + 删除会把 worker 永久卡住。
        self._compress_events: dict[str, threading.Event] = {}
        # V0.8.21 E3：世代票据（epoch token）。
        #
        # 要解决的竞态：worker 在 run() 尾声才发 finished，但 ffmpeg 早在几百毫秒
        # 前就被 cancel_event 叫停了。这个窗口期里用户完全来得及「取消 → 立刻重试」
        # ——于是同一个 task.id 上会同时存在两代 worker。老 worker 的 finished 一到，
        # _on_finished 就会把**新**这一代的 event / worker 一起 pop 掉、把正在
        # RUNNING 的任务标成 FAILED、还把新 worker 扔进 _retired 等着被释放，
        # 也就是那条"WorkerSignals has been deleted"崩溃的正牌成因之一。
        #
        # 票据规则：每次 _launch 发一张递增的票，记在这里；worker 的所有回调都
        # 带着自己那张票回来，票号对不上就是上一代的回声，直接丢弃。
        # clear() 只需把这张表清空，所有在途回调就全部失效——比逐个去追 worker
        # 干净得多。
        self._epoch = 0
        self._task_epoch: dict[str, int] = {}
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

    # --- V0.8.21 E4：子进程句柄登记与真暂停 ---
    def register_proc(self, task_id: str, proc) -> None:
        """worker 线程回调：登记 / 注销某任务当前的 ffmpeg 子进程。

        Args:
            task_id: 任务 id。
            proc: ``subprocess.Popen``；传 ``None`` 表示进程已结束，注销登记。

        Notes:
            **这个方法会被 worker 线程调用**，不是 GUI 线程，所以全程持锁且
            不碰任何 Qt 对象。硬件降级重试会起第二个进程，按「后来者覆盖」
            处理即可。

            登记瞬间如果队列已经处于暂停态，新起的进程要立刻挂起——否则
            「暂停中点了重试 / 暂停前一刹那刚启动的任务」会带着一个满速跑的
            ffmpeg 溜过去，用户看到的就是「按了暂停，风扇还在狂转」。
        """
        with self._procs_lock:
            if proc is None:
                self._procs.pop(task_id, None)
                return
            self._procs[task_id] = proc
            paused = self._paused
        if paused:
            proc_control.suspend(proc)

    def _suspend_all(self) -> int:
        """挂起全部在跑的子进程，返回成功挂起的个数。"""
        with self._procs_lock:
            procs = list(self._procs.values())
        return sum(1 for p in procs if proc_control.suspend(p))

    def _resume_all(self) -> None:
        """恢复全部被挂起的子进程。"""
        with self._procs_lock:
            procs = list(self._procs.values())
        for p in procs:
            proc_control.resume(p)

    def pause(self) -> None:
        """暂停队列。

        V0.8.21 E4 之前这里只是「不再派发新任务」的软暂停——正在转码的
        ffmpeg 照样吃满 CPU/GPU 跑到底。转一个 4K 长视频时，用户按下暂停后
        机器该卡还是卡，等于没暂停。

        现在改为：置暂停位（拦住后续派发）**并**用 psutil 挂起所有在跑的
        ffmpeg 进程树。psutil 不可用时自动退回旧的软暂停语义，功能不受影响。
        """
        self._paused = True
        n = self._suspend_all()
        if n:
            log.info("队列暂停：已挂起 %d 个子进程", n)
        self.state_changed.emit()

    def resume(self) -> None:
        """从暂停状态恢复：先解挂已有进程，再继续派发新任务。

        顺序很重要——先 ``_resume_all()`` 再 ``_fill_slots()``。反过来的话，
        新任务会和还挂着的老任务抢线程池槽位，出现「恢复后前几个任务纹丝
        不动」的怪象。
        """
        if not self._paused:
            return
        self._paused = False
        self._resume_all()
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
        # E3：任务已不在队列里，作废它的票据，后续回调不再改任何状态
        self._task_epoch.pop(task_id, None)
        self.tasks = [t for t in self.tasks if t.id != task_id]
        self.queue_changed.emit()

    def clear(self) -> None:
        """清空整个队列，并给所有在跑任务置取消标志。"""
        # V0.8.21 E4：先全部解挂。被 psutil 挂起的进程停在 readline 上，
        # 永远走不到 cancel_event 的检查点——不解挂就是清空后进程全变僵尸。
        self._resume_all()
        for event in self._events.values():
            event.set()
        for cevent in self._compress_events.values():
            cevent.set()
        # v0.8.2 Bug3：清空时一并停掉所有假进度追踪，避免定时器空转。
        for task in self.tasks:
            self._fake.stop(task.id)
        # V0.8.21 E3：撕掉全部票据。在途 worker 的回调随后一律作废，不会再回来
        # 改状态、也不会替一个已经被清空的队列继续 _fill_slots
        # （旧实现里，清空后新拖进来的文件会被这类回声悄悄自动启动）。
        self._task_epoch.clear()
        self.tasks.clear()
        self._running = False
        self._paused = False
        self.queue_changed.emit()
        self.state_changed.emit()

    def cancel_task(self, task_id: str) -> None:
        """给指定任务置取消标志，worker 会在下一个检查点自行退出。

        V0.8.21 E4：置标志前必须先解挂子进程。挂起态的 ffmpeg 不产出任何
        输出，worker 会一直卡在 ``proc.stdout.readline()``，压根轮不到检查
        ``cancel_event``——「暂停后删任务」会永久卡住那一行。
        """
        with self._procs_lock:
            proc = self._procs.get(task_id)
        if proc is not None:
            proc_control.resume(proc)
        event = self._events.get(task_id)
        if event:
            event.set()
        # 压缩阶段用的是另一套 event（见 _on_finished），一并置位
        cevent = self._compress_events.get(task_id)
        if cevent:
            cevent.set()
        task = self.get_task(task_id)
        if task and task.status in (Task.RUNNING, Task.COMPRESSING):
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
        """把空闲的并发槽位补满待处理任务；暂停 / 未启动时不派发任何新任务。

        Notes:
            V0.8.21 E3 补上 ``_running`` 这道闸。此前只看 ``_paused``，于是
            「清空队列 → 拖入新文件 → 还没点开始」这一串操作里，只要有一个上
            一代 worker 姗姗来迟地报完成，就会顺手把新文件派发出去 —— 用户没
            按开始，任务自己跑了。
        """
        self._drain_retired()
        if self._paused or not self._running:
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

        # E3：发一张新票，本次投递的所有回调都带着它回来
        self._epoch += 1
        ep = self._epoch
        self._task_epoch[task.id] = ep

        worker = ConversionWorker(task, self.ffmpeg_path, self.hw, event, self)
        worker.epoch = ep
        s = worker.signals
        # partial 把票号绑在最前面，信号自带的参数依次跟在后面。
        # 不用 lambda：lambda 会把闭包变量按引用捕获，循环里连多个 worker 时
        # 全都会指向最后一次的 ep（经典闭包坑）。partial 是值绑定，天然安全。
        s.started.connect(partial(self._ep_started, ep))
        s.progress.connect(partial(self._ep_progress, ep))
        s.finished.connect(partial(self._ep_finished, ep))
        s.compress_started.connect(partial(self._ep_relay, ep, self.compress_started))
        s.compress_finished.connect(partial(self._ep_relay, ep, self.compress_finished))
        s.stats.connect(partial(self._ep_stats, ep))

        prev = self._workers.get(task.id)
        if prev is not None:
            # 同一个 id 上还挂着上一代 worker（取消后立刻重试）。直接覆盖会丢掉
            # 它最后一个 Python 引用，转进 _retired 让它按既有节奏被释放。
            self._retired.append(prev)
        self._workers[task.id] = worker  # 持有引用防 GC
        self._pool.start(worker)
        # v0.8.2 Bug3：启动假进度条。任务刚被派发就开始涨（用户感知的
        # 「任务开始」）。v0.8.21 起 ffmpeg 已能吐出真实进度（见
        # core/ffmpeg_progress.py），假进度只作为「真进度到来前」的兜底，
        # 合并策略仍是 max(fake, real)，保证不卡 0%、也不会倒退。
        self._fake.start(task.id, estimate_seconds(task))

    # --- V0.8.21 E3：世代票据守卫 ---
    def _is_fresh(self, task_id: str, epoch: int) -> bool:
        """该回调是否来自这个任务**当前**这一代 worker。

        Args:
            task_id: 任务 id。
            epoch: 回调携带的票号。
        Returns:
            票号与登记在案的一致为 ``True``；任务已被清空 / 移除，或已被更
            新的一代覆盖，都返回 ``False``。
        """
        return self._task_epoch.get(task_id) == epoch

    def _ep_started(self, epoch: int, task_id: str) -> None:
        if self._is_fresh(task_id, epoch):
            self._on_started(task_id)

    def _ep_progress(self, epoch: int, task_id: str, pct: int) -> None:
        if self._is_fresh(task_id, epoch):
            self._on_progress(task_id, pct)

    def _ep_stats(self, epoch: int, task_id: str, snap) -> None:
        if self._is_fresh(task_id, epoch):
            self.task_stats.emit(task_id, snap)

    def _ep_relay(self, epoch: int, sig, task_id: str) -> None:
        """把 worker 的信号原样转发到 manager 的同名公开信号（带票据过滤）。"""
        if self._is_fresh(task_id, epoch):
            sig.emit(task_id)

    def _ep_compress_progress(self, epoch: int, task_id: str, pct: int) -> None:
        if self._is_fresh(task_id, epoch):
            self._on_compress_progress(task_id, pct)

    def _ep_finished(self, epoch: int, task_id: str, ok: bool, log_text: str) -> None:
        if self._is_fresh(task_id, epoch):
            self._on_finished(task_id, ok, log_text)
            return
        self._discard_stale(task_id, epoch)

    def _ep_compress_finished(self, epoch: int, task_id: str) -> None:
        if self._is_fresh(task_id, epoch):
            self._on_compress_finished(task_id)
            return
        # 压缩 worker 的残留另放一张表，回收逻辑与转换 worker 对称
        log.debug("丢弃过期的 compress_finished 回调：task=%s epoch=%s", task_id, epoch)
        cw = self._compress_workers.get(task_id)
        if cw is None or getattr(cw, "epoch", None) != epoch:
            return
        self._compress_workers.pop(task_id, None)
        self._retired.append(cw)
        self._compress_events.pop(task_id, None)
        with self._procs_lock:
            self._procs.pop(task_id, None)

    def _discard_stale(self, task_id: str, epoch: int) -> None:
        """处置一条过期的 finished：只回收属于这一代的残留，别碰新一代。

        Args:
            task_id: 任务 id。
            epoch: 过期回调携带的票号。

        Notes:
            两种过期来源，处置方式不同：

            - **队列被清空**：``_task_epoch`` 里已经没这个 id 了，但
              ``_workers`` / ``_events`` / ``_procs`` 里还挂着这一代的残留，
              需要在这里收干净，否则会一直留到进程退出。
            - **被新一代覆盖**（取消后立刻重试）：表里存的是新 worker，
              它正跑得好好的，**一个字段都不能动**。靠比对 ``worker.epoch``
              把两种情况区分开。
        """
        log.debug("丢弃过期的 finished 回调：task=%s epoch=%s", task_id, epoch)
        w = self._workers.get(task_id)
        if w is None or getattr(w, "epoch", None) != epoch:
            return  # 表里是新一代，不是这条回调的东西
        self._workers.pop(task_id, None)
        self._retired.append(w)
        self._events.pop(task_id, None)
        with self._procs_lock:
            self._procs.pop(task_id, None)
        # 「删掉一个正在跑的任务」也走这条路：它占的槽位刚刚空出来，得有人把
        # 后面排队的补上，否则并发数会随着删除次数一路缩水。_fill_slots 自带
        # _running / _paused 双闸，清空后的回声进不来。
        self._fill_slots()

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

    def _on_compress_progress(self, task_id: str, pct: int) -> None:
        """压缩 worker 报告真实进度 → 交给假进度条合并（通道为 compress）。

        与 :meth:`_on_progress` 同构：合并结果由
        ``_fake.compress_progress_changed`` → :meth:`_on_fake_compress_progress`
        统一发出，避免同一帧发两次 ``compress_progress``。
        """
        self._fake.merge(task_id, pct)

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
        if not need_compress:
            # 兜底注销：正常路径由 on_proc(None) 清掉，但 ffmpeg 启动失败
            # （OSError）时那条回调根本没机会跑，句柄会一直挂在表里。
            with self._procs_lock:
                self._procs.pop(task_id, None)
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
            cevent = threading.Event()
            self._compress_events[task.id] = cevent
            cw = CompressWorker(task, self, cevent)  # signals parent=manager
            # E3：压缩阶段沿用转换阶段那张票 —— 它是同一个任务的后半程，
            # 一旦任务被清空 / 移除，两段的回调应当一起失效。
            ep = self._task_epoch.get(task.id, 0)
            cw.epoch = ep
            cs = cw.signals
            cs.compress_started.connect(partial(self._ep_relay, ep, self.compress_started))
            cs.compress_finished.connect(partial(self._ep_compress_finished, ep))
            # v0.8.21 D1：压缩阶段也有真实进度了（ffmpeg -progress / Pillow 分段），
            # 走 _fake.merge 与假进度合并，保证 max(fake, real) 且不回退。
            cs.compress_progress.connect(partial(self._ep_compress_progress, ep))
            cs.stats.connect(partial(self._ep_stats, ep))
            self._compress_workers[task.id] = cw  # 持有引用防 GC
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
        self._compress_events.pop(task_id, None)
        with self._procs_lock:
            self._procs.pop(task_id, None)
        cw = self._compress_workers.pop(task_id, None)
        if cw is not None:
            self._retired.append(cw)  # 与转换 worker 共用延迟释放池
        self.compress_finished.emit(task_id)
        if self._running_count() == 0:
            self._running = False
        self.state_changed.emit()
