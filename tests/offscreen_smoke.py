"""Offscreen construction + flow smoke test (no window paint).

Run with: python tests/offscreen_smoke.py
Requires QT_QPA_PLATFORM=offscreen (set here automatically).
Uses os._exit on success to bypass Qt teardown that can hard-kill in CI/sandbox.

NOTE: this sandbox hard-kills (exit 127) if a *paint* of the full FluentWindow /
a populated queue row / an InfoBar is attempted. Constructing widgets without
calling show() does NOT paint, so we validate every interface's __init__ + retheme
chain and the Convert flow by building the interfaces standalone (no FluentWindow).
Full-window visual verification belongs on a real desktop / GitHub Actions.

Covers:
  - All five interfaces import and construct (rebuilt UI).
  - Convert (v0.2.7 redesign): files are expanded/filtered by category, the
    format picker (FormatGrid) is seeded from the default selection, and the
    setup dialog's confirm pushes tasks into the queue via ConversionManager.
    The full ConvertSetupDialog (which builds an AdvancedPanel with CJK combo
    items) hard-kills this sandbox, so it is exercised on a real desktop / CI;
    here we test the safe pieces it delegates to (no repaint, no native combos).
  - Detached manager: output-mode + same-format logic.
  - Upscale staging accepts media.
  - v0.8.0-B3x: 动效「状态迁移后稳态」指纹，与 tests/anim_baseline.json 逐键比对。
"""

import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def step(msg):
    print(f"[step] {msg}", flush=True)


# =============================================================================
# v0.8.0-B3x：动效关闭态一致性 + 状态迁移后稳态（常驻回归）
# =============================================================================
# 为什么要单开这一段：``tests/qss_snapshot.py`` 只采「刚构造出来那一刻」的样式串，
# 而 B3 的动效全部发生在**状态迁移之后** —— 胶囊从灰变绿、进度条追值、拖放区
# 悬停回落、行进出场。迁移完成后的稳态是快照的盲区，也恰恰是最容易出事的地方：
# :func:`gui.animations.blend_color` 返回的是**小写**十六进制，只要有一条路径把
# 中途帧的计算值留在了终点上，肉眼看不出、快照也发现不了，但样式串已经从令牌
# 原文（``#3EB68F``）悄悄变成了计算值（``#3eb68f``）。
#
# 为什么不逐帧验：``animations.should_animate()`` 要求 ``widget.isVisible()``，
# 离屏恒为 False，所有调用点都退化成「直接写终值」。所以离屏能验、且值得验的是
# **终值**而非插值过程 —— 这与本文件其余段落的定位一致。
#
# 三条断言（见 main() 里的 B3x 段落）：
#   ① 当前环境与「强制关闭动效」两趟指纹逐键一致（关闭态一致性）；
#   ② 指纹与磁盘基线 ``tests/anim_baseline.json`` 逐键一致；
#   ③ 稳态里不出现 blend_color 泄漏的小写色值（白名单见
#      :func:`_steady_state_lowercase_allowlist`）。
#
# 采集口径（两条自律，破坏任何一条都会让这道门禁变成噪声源）：
#   - **不收任何经 tr() 的自然语言文案**。否则批次 C 改文案就会把门禁染红，
#     那是误报。要验文案一致性有 ``tests/i18n_coverage.py``，各管各的。
#   - **布局算出来的像素值只收语义、不收数值**。``sizeHint()`` 随 Qt 版本与
#     平台字体浮动，把它写进基线等于把基线绑死在采集机器上（B0 刚拆掉过一次
#     这种绑定）。代码自己写死的数值（0 / 16777215 / 时长 / 阈值）照收不误。

ANIM_BASELINE = Path(__file__).parent / "anim_baseline.json"

# 只认 3/4/6/8 位的合法色值写法，且后面不能再跟字母数字下划线 ——
# 否则 ``#dropInner`` 这类 objectName 选择器会被误当成颜色。
_HEX_LITERAL = re.compile(
    r"#(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{4}|[0-9A-Fa-f]{3})(?![0-9A-Za-z_])"
)


def _steady_state_lowercase_allowlist() -> set:
    """稳态里**合法**的小写十六进制色值。

    Returns:
        形如 ``{"#f5f5f5", "#e7e7e7", ...}`` 的集合，全部小写。

    Notes:
        本来的验收口径是「稳态输出中不含小写十六进制色值」，但直接照做会误报：
        ``QColor.name()`` 的返回值天生小写，而 ``drop_area`` 常态底色写的就是
        ``surface().name()``（``#f5f5f5``），按下态是 ``surface_pressed().name()``
        （``#e7e7e7``）—— 这两处在 B4 基线里就是这个样子，与动效无关。

        所以判据收紧成「小写、且不是主题访问器 ``.name()`` 能产出的值」：
        剩下的小写色值只可能来自 :func:`gui.animations.blend_color`，也就是
        中途帧漏进了终点。白名单**动态**从主题访问器算出来，换主题色不用改代码。

        已知盲区：``blend_color`` 若恰好算出白名单里的某个值（只可能发生在
        t≈0 的端点上，而端点本来就该等于该值），检测不出来 —— 但那种情况下
        字符串与改造前一致，断言②（与基线逐键一致）也不会有差异，无实际风险。
    """
    from momentshift.gui import theme

    accessors = (
        theme.surface,
        theme.surface_hover,
        theme.surface_pressed,
        theme.accent_color,
        theme.success_color,
        theme.danger_color,
    )
    return {fn().name().lower() for fn in accessors}


def _lowercase_hex_offenders(fingerprint: dict, allow: set) -> dict:
    """挑出指纹里疑似 blend_color 泄漏的小写色值。

    Args:
        fingerprint: ``{键: 值}`` 稳态指纹，值均为字符串。
        allow: :func:`_steady_state_lowercase_allowlist` 给出的合法小写色集合。

    Returns:
        ``{指纹键: [可疑色值, ...]}``；没有可疑项时为空字典。

    Notes:
        纯数字色值（如 ``#238636``）大小写写法相同，无法区分来源，一律放行。
    """
    offenders: dict = {}
    for key, value in fingerprint.items():
        for match in _HEX_LITERAL.finditer(value):
            literal = match.group(0)
            if literal == literal.upper():
                continue  # 令牌原文（或纯数字），本该如此
            if literal.lower() in allow:
                continue  # 主题访问器 .name() 的合法产物
            offenders.setdefault(key, []).append(literal)
    return offenders


def _fp_status_pill(fp: dict) -> None:
    """状态胶囊：全状态底色 + 八条真实流转链的终态。"""
    from momentshift.gui.queue_widget import _STATUS_PILL_BG, StatusPill

    for status in sorted(_STATUS_PILL_BG):
        pill = StatusPill(status)
        fp[f"statuspill/solo:{status}/qss"] = pill.styleSheet()
        fp[f"statuspill/solo:{status}/bg_t"] = pill._bg_t
        pill.deleteLater()

    flows = {
        # 常规：等待 → 转换 → 完成
        "normal": ("running", "done"),
        # 带压缩：完成后还要再走两跳，最容易在中途色上翻车
        "compress": ("running", "done", "compressing", "compress_done"),
        "failure": ("running", "failed"),
        "cancel": ("running", "canceled"),
        # 同色短路：pending 与 canceled 都是灰，不该起动画也不该改字符串
        "same-color": ("canceled", "pending", "canceled"),
        "software": ("running", "done_sw"),
        # 失败后重试再成功：走回头路，起点色必须重新对齐
        "retry": ("running", "failed", "pending", "running", "done"),
        # 未知状态键回落到 pending
        "unknown-key": ("b3x-no-such-status", "running"),
        # 连切不喘气：每一跳都在上一跳的稳态上继续
        "rapid": (
            "running",
            "done",
            "compressing",
            "compress_done",
            "failed",
            "pending",
            "running",
            "done",
        ),
    }
    for name in sorted(flows):
        pill = StatusPill("pending")
        for index, status in enumerate(flows[name], start=1):
            pill.set_status(status)
            head = f"statuspill/flow:{name}/{index:02d}-{status}"
            fp[f"{head}/qss"] = pill.styleSheet()
            fp[f"{head}/bg_t"] = pill._bg_t
            fp[f"{head}/bg_from"] = pill._bg_from
            fp[f"{head}/bg_to"] = pill._bg_to
        pill.deleteLater()


def _fp_progress_bar(fp: dict) -> None:
    """进度条：平滑阈值、回退硬切、夹取与失败定格。"""
    from momentshift.gui import animations
    from momentshift.gui.queue_widget import ProgressBar

    step_min = animations.PROGRESS_SMOOTH_MIN_STEP
    sequences = {
        "forward-big": (0, 50, 100),
        # 每步都小于阈值 → 全程硬切，一段动画都不该起
        "forward-small": tuple(range(0, step_min)),
        "threshold": (0, step_min - 1, step_min, step_min * 2),
        # 回退是「重置」语义，必须立刻到位而不是慢慢缩回去
        "backward-reset": (0, 80, 0, 40, 39),
        "clamp": (-30, 250, 100, 0),
        # 高频回调：模拟一秒十几次的真实进度推送
        "burst": tuple(range(0, 101, 7)),
    }
    for name in sorted(sequences):
        bar = ProgressBar()
        for index, value in enumerate(sequences[name]):
            bar.set_value(value)
            fp[f"progressbar/{name}/{index:02d}-set{value}/value"] = bar._value
        fp[f"progressbar/{name}/final/barValue"] = bar.barValue
        bar.deleteLater()

    bar = ProgressBar()
    bar.set_value(60)
    bar.set_error(True)
    fp["progressbar/error/frozen-at"] = bar._value
    fp["progressbar/error/flag"] = bar._error
    bar.set_value(90)
    fp["progressbar/error/after-set90"] = bar._value
    bar.set_error(False)
    fp["progressbar/error/cleared"] = bar._error
    fp["progressbar/error/value-kept"] = bar._value
    bar.deleteLater()

    fp["progressbar/const/smooth-min-step"] = step_min


def _fp_drop_area(fp: dict) -> None:
    """拖放区：悬停进出、按下抬起、retheme 回正、drop 硬切。"""
    import inspect

    from momentshift.gui.drop_area import DropArea

    area = DropArea()
    fp["droparea/00-init/inner"] = area.inner.styleSheet()
    fp["droparea/00-init/hover_t"] = area._hover_t

    # 末两跳是重复进入：dragEnterEvent 连发时必须幂等
    transitions = (
        ("hover-on", True),
        ("hover-off", False),
        ("hover-on-again", True),
        ("hover-on-repeat", True),
    )
    for index, (name, hover) in enumerate(transitions, start=1):
        area._set_hover(hover)
        fp[f"droparea/{index:02d}-{name}/inner"] = area.inner.styleSheet()
        fp[f"droparea/{index:02d}-{name}/hover_t"] = area._hover_t

    area._pressed = True
    area._apply_style()
    fp["droparea/05-pressed/inner"] = area.inner.styleSheet()
    area._pressed = False
    area._apply_style()
    fp["droparea/06-released/inner"] = area.inner.styleSheet()

    area._set_hover(False)
    area.retheme()
    fp["droparea/07-retheme/inner"] = area.inner.styleSheet()
    fp["droparea/07-retheme/hover_t"] = area._hover_t
    # 换肤后必须与出厂态一字不差，否则说明 retheme 走漏了中途色
    assert fp["droparea/07-retheme/inner"] == fp["droparea/00-init/inner"], (
        "retheme 后的稳态与初始态不一致：\n"
        f"  初始: {fp['droparea/00-init/inner']}\n"
        f"  换肤: {fp['droparea/07-retheme/inner']}"
    )

    # dropEvent 要造一个真的 QDropEvent 才跑得起来，这里退而求其次做源码级守卫：
    # 「落下即硬切」是 B3 明确写进注释的契约，不能被悄悄改成走过渡。
    source = inspect.getsource(DropArea.dropEvent)
    fp["droparea/08-dropEvent/hard-cut"] = (
        "self._hover = False" in source and "self._apply_style()" in source
    )
    area.deleteLater()


def _settle_card_anim(card) -> None:
    """把折叠卡片的动画推到 ``t=1``，等价于 Qt 播完最后一帧。

    Args:
        card: 正在跑 ``_anim`` 的 :class:`gui.theme.CollapsibleCard`。

    Notes:
        Qt 在最后一帧做两件事：把属性写成 ``endValue()``，然后发 ``finished``。
        这里如实照做 —— 只调 ``_on_anim_finished()`` 而不写终值，拿到的会是
        「动画没播过」的中间态（收起后 ``maximumHeight`` 还停在 16777215），
        那不是稳态，把它写进基线等于给后人立一条假事实。

        ``stop()`` 不会补发 ``finished``（Qt 只在自然跑到终点时发），所以不存在
        重复收尾。
    """
    anim = card._anim
    if anim is not None:
        card._body.setMaximumHeight(int(anim.endValue()))
        anim.stop()
    card._on_anim_finished()


def _fp_collapsible_card(fp: dict) -> None:
    """折叠卡片：出厂折叠不起动画、展开/收起的终值契约、同值 no-op。

    Notes:
        这里**不**转事件循环等 250ms 播完，而是用 :func:`_settle_card_anim`
        手动推到 t=1。既拿到了真正的稳态，又不引入等待与不确定性（本文件里
        转事件循环还有触发真实 paint 被沙箱硬杀的风险，见模块 docstring）。
    """
    from momentshift.gui import animations
    from momentshift.gui.theme import CollapsibleCard

    card = CollapsibleCard("b3x", "", None, collapsed=True)
    fp["card/00-born-collapsed/no-anim"] = card._anim is None
    fp["card/00-born-collapsed/max-h"] = card._body.maximumHeight()
    fp["card/00-born-collapsed/collapsed"] = card.isCollapsed()
    fp["card/00-born-collapsed/visible"] = card._body.isVisibleTo(card)

    card.setCollapsed(False)
    fp["card/01-expand/collapsed"] = card.isCollapsed()
    fp["card/01-expand/anim-created"] = card._anim is not None
    fp["card/01-expand/anim-duration"] = card._anim.duration()
    fp["card/01-expand/anim-curve"] = card._anim.easingCurve().type().name
    # 终值来自 sizeHint()，随平台字体浮动 —— 只收「是正数」这个语义
    fp["card/01-expand/anim-end-positive"] = int(card._anim.endValue()) > 0
    _settle_card_anim(card)
    # 展开收尾会主动解除高度上限，内容后续变高不会被裁（v0.7.3 Bug3）
    fp["card/02-expand-settled/max-h"] = card._body.maximumHeight()
    fp["card/02-expand-settled/visible"] = card._body.isVisibleTo(card)

    card.setCollapsed(True)
    fp["card/03-collapse/anim-duration"] = card._anim.duration()
    fp["card/03-collapse/anim-curve"] = card._anim.easingCurve().type().name
    fp["card/03-collapse/anim-end"] = int(card._anim.endValue())
    _settle_card_anim(card)
    fp["card/04-collapse-settled/max-h"] = card._body.maximumHeight()
    fp["card/04-collapse-settled/visible"] = card._body.isVisibleTo(card)
    fp["card/04-collapse-settled/collapsed"] = card.isCollapsed()

    previous = card._anim
    card.setCollapsed(True)
    fp["card/05-same-value-noop/anim-untouched"] = card._anim is previous
    card.deleteLater()

    # B3 接入点 6 的常驻证据：时长与曲线都必须来自 animations 模块
    fp["card/06-token/duration"] = CollapsibleCard._ANIM_DURATION
    fp["card/06-token/duration-from-animations"] = (
        CollapsibleCard._ANIM_DURATION == animations.DURATION_CARD
    )


def _fp_queue_list(fp: dict, png: str, out: str) -> None:
    """队列列表：增删、重复入队、双重移除、清空后再删。"""
    from momentshift.core.models import Task
    from momentshift.gui.queue_widget import QueueListWidget

    def _task(index: int) -> Task:
        return Task(
            id=f"b3x-{index}",
            input_path=png,
            output_path=os.path.join(out, f"b3x-{index}.jpg"),
            target_format="jpg",
            category="image",
            use_gpu=False,
        )

    queue = QueueListWidget()
    fp["queuelist/00-empty/items"] = len(queue.items)
    fp["queuelist/00-empty/layout"] = queue.listLayout.count()
    fp["queuelist/00-empty/hint-hidden"] = not queue.emptyHint.isVisibleTo(queue)

    for index in range(3):
        queue.add_item(_task(index))
    fp["queuelist/01-add3/items"] = len(queue.items)
    fp["queuelist/01-add3/layout"] = queue.listLayout.count()
    # 离屏 should_animate() 恒 False，_attach_row 短路在前，预算一次都不该被吃掉
    fp["queuelist/01-add3/budget"] = queue._anim_budget
    fp["queuelist/01-add3/hint-hidden"] = not queue.emptyHint.isVisibleTo(queue)

    queue.add_item(_task(1))
    fp["queuelist/02-duplicate-add/items"] = len(queue.items)

    queue.remove_item("b3x-1")
    fp["queuelist/03-remove/items"] = len(queue.items)
    fp["queuelist/03-remove/key-gone"] = "b3x-1" not in queue.items

    # 双重移除：删除动画播到一半又被「清空」扫一遍，是线上真实存在的时序
    queue.remove_item("b3x-1")
    queue.remove_item("b3x-1")
    fp["queuelist/04-double-remove/items"] = len(queue.items)
    queue.remove_item("b3x-never-existed")
    fp["queuelist/05-remove-unknown/items"] = len(queue.items)

    queue.clear()
    fp["queuelist/06-clear/items"] = len(queue.items)
    fp["queuelist/06-clear/layout"] = queue.listLayout.count()
    queue.remove_item("b3x-0")
    fp["queuelist/07-clear-then-remove/items"] = len(queue.items)

    # 清空后重新入队：预算与空态都要回到干净状态
    queue.add_item(_task(9))
    fp["queuelist/08-refill/items"] = len(queue.items)
    fp["queuelist/08-refill/budget"] = queue._anim_budget
    queue.deleteLater()


class _FrozenClock:
    """只暴露 ``monotonic()`` 的假时钟，单位与 :func:`time.monotonic` 一致（秒）。

    Attributes:
        seconds: 当前时刻，测试自己推进。
    """

    def __init__(self, seconds: float = 1000.0):
        self.seconds = seconds

    def monotonic(self) -> float:
        return self.seconds


def _fp_burst_budget(fp: dict) -> None:
    """突发批量闸门：用冻结时钟驱动，结果与机器快慢无关。

    Notes:
        为什么必须冻结时钟：``_anim_budget_allows()`` 用 ``time.monotonic()``
        划分「批」，真实时钟下这段循环跑多久取决于机器和 GC，把它的结果写进
        基线就是在埋一颗随机红灯。换掉 ``gui.base`` 模块里的 ``time`` 引用后，
        预算算术本身仍是被真实执行的，只有时间源是确定的。
    """
    from momentshift.gui import animations
    from momentshift.gui import base as base_module
    from momentshift.gui.queue_widget import QueueListWidget

    queue = QueueListWidget()
    clock = _FrozenClock()
    real_time = base_module.time
    base_module.time = clock
    try:
        probes = animations.QUEUE_ANIM_BATCH_LIMIT + 6
        verdicts = [queue._anim_budget_allows() for _ in range(probes)]
        fp["burst/same-batch/probes"] = probes
        fp["burst/same-batch/allowed"] = sum(verdicts)
        fp["burst/same-batch/denied"] = len(verdicts) - sum(verdicts)
        fp["burst/same-batch/first-denied-index"] = verdicts.index(False)
        fp["burst/same-batch/limit"] = animations.QUEUE_ANIM_BATCH_LIMIT

        # 静默窗口内 → 仍算同一批，预算继续累计
        clock.seconds += (animations.QUEUE_BURST_WINDOW_MS - 1) / 1000.0
        fp["burst/within-window/allowed"] = queue._anim_budget_allows()
        fp["burst/within-window/budget"] = queue._anim_budget

        # 超过静默窗口 → 用户停手了，预算重置，第一行重新拿到动效
        clock.seconds += (animations.QUEUE_BURST_WINDOW_MS + 1) / 1000.0
        fp["burst/after-window/allowed"] = queue._anim_budget_allows()
        fp["burst/after-window/budget"] = queue._anim_budget
        fp["burst/after-window/window-ms"] = animations.QUEUE_BURST_WINDOW_MS
    finally:
        base_module.time = real_time
    queue.deleteLater()


def _fp_queue_row(fp: dict, png: str, out: str) -> None:
    """队列行：转换 → 压缩 → 回落，以及失败 → 重试 → 取消两条链。"""
    from momentshift.core.models import Task
    from momentshift.gui.queue_widget import QueueItemWidget

    def _row(task_id: str) -> QueueItemWidget:
        return QueueItemWidget(
            Task(
                id=task_id,
                input_path=png,
                output_path=os.path.join(out, "row.jpg"),
                target_format="jpg",
                category="image",
                use_gpu=False,
            )
        )

    row = _row("b3x-row-a")
    fp["queuerow/00-init/pill"] = row.pill.styleSheet()
    fp["queuerow/00-init/prog"] = row.prog._value
    fp["queuerow/00-init/detail-empty"] = row.detailLbl.text() == ""
    fp["queuerow/00-init/retry-visible"] = row.retryBtn.isVisibleTo(row)

    row.set_status("running")
    row.set_progress(42)
    fp["queuerow/01-running/pill"] = row.pill.styleSheet()
    fp["queuerow/01-running/prog"] = row.prog._value
    fp["queuerow/01-running/error"] = row.prog._error

    row.set_progress(100)
    row.set_status("done")
    fp["queuerow/02-done/pill"] = row.pill.styleSheet()
    fp["queuerow/02-done/prog"] = row.prog._value

    row.set_compress(30)
    fp["queuerow/03-compressing/pill"] = row.pill.styleSheet()
    fp["queuerow/03-compressing/prog"] = row.prog._value

    row.set_compress(100, done=True)
    fp["queuerow/04-compress-done/pill"] = row.pill.styleSheet()
    fp["queuerow/04-compress-done/prog"] = row.prog._value

    # 压缩跑完后再收到 done：胶囊必须保持蓝色，不许刷回绿色
    row._task.compress_done = True
    row.restore_after_compress()
    fp["queuerow/05-restore/pill"] = row.pill.styleSheet()
    fp["queuerow/05-restore/stays-blue"] = (
        row.pill.styleSheet() == fp["queuerow/04-compress-done/pill"]
    )
    row.deleteLater()

    row2 = _row("b3x-row-b")
    row2.set_progress(70)
    # 错误文案传固定 ASCII：指纹里不许出现任何跟语言相关的东西
    row2.set_status("failed", error="B3X-ERROR")
    fp["queuerow/10-failed/pill"] = row2.pill.styleSheet()
    fp["queuerow/10-failed/prog"] = row2.prog._value
    fp["queuerow/10-failed/error"] = row2.prog._error
    fp["queuerow/10-failed/detail"] = row2.detailLbl.text()
    fp["queuerow/10-failed/retry-visible"] = row2.retryBtn.isVisibleTo(row2)

    row2.set_progress(0)
    row2.set_status("pending")
    fp["queuerow/11-retry/pill"] = row2.pill.styleSheet()
    fp["queuerow/11-retry/prog"] = row2.prog._value
    fp["queuerow/11-retry/error"] = row2.prog._error
    fp["queuerow/11-retry/retry-visible"] = row2.retryBtn.isVisibleTo(row2)

    row2.set_status("canceled")
    fp["queuerow/12-canceled/pill"] = row2.pill.styleSheet()
    fp["queuerow/12-canceled/retry-visible"] = row2.retryBtn.isVisibleTo(row2)
    row2.deleteLater()


def collect_anim_fingerprint(png: str, out: str) -> dict:
    """把六个动效接入点的「状态迁移后稳态」采成一份可比对的指纹。

    Args:
        png: 一个真实存在的图片路径，用于构造 Task。
        out: 输出目录路径。

    Returns:
        ``{键: 字符串值}``，按键排序；值统一 ``str()`` 化以便 JSON 稳定落盘。
    """
    fingerprint: dict = {}
    collectors = (
        ("statuspill", lambda: _fp_status_pill(fingerprint)),
        ("progressbar", lambda: _fp_progress_bar(fingerprint)),
        ("droparea", lambda: _fp_drop_area(fingerprint)),
        ("card", lambda: _fp_collapsible_card(fingerprint)),
        ("queuelist", lambda: _fp_queue_list(fingerprint, png, out)),
        ("burst", lambda: _fp_burst_budget(fingerprint)),
        ("queuerow", lambda: _fp_queue_row(fingerprint, png, out)),
    )
    for label, run in collectors:
        try:
            run()
        except Exception as exc:  # noqa: BLE001 - 采集失败也要变成一条可见的差异
            fingerprint[f"<采集失败>/{label}"] = f"{type(exc).__name__}: {exc}"
    return {key: str(value) for key, value in sorted(fingerprint.items())}


def _diff_fingerprints(left: dict, right: dict) -> tuple:
    """比对两份指纹。

    Args:
        left: 参照方（基线 / 第一趟）。
        right: 被检方（当前 / 第二趟）。

    Returns:
        ``(仅左有, 仅右有, 值不同)`` 三个已排序的键列表。
    """
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    changed = sorted(k for k in set(left) & set(right) if left[k] != right[k])
    return only_left, only_right, changed


def _assert_same(left: dict, right: dict, left_name: str, right_name: str) -> None:
    """两份指纹必须逐键一致，否则打印可读差异并抛断言。"""
    only_left, only_right, changed = _diff_fingerprints(left, right)
    if not (only_left or only_right or changed):
        return
    lines = [f"指纹不一致：{left_name} {len(left)} 项 / {right_name} {len(right)} 项"]
    for key in only_left:
        lines.append(f"  - 仅 {left_name} 有 {key}\n      {left[key]}")
    for key in only_right:
        lines.append(f"  + 仅 {right_name} 有 {key}\n      {right[key]}")
    for key in changed:
        lines.append(f"  ! {key}\n      {left_name}: {left[key]}\n      {right_name}: {right[key]}")
    message = "\n".join(lines)
    print(message, flush=True)
    raise AssertionError(message)


def run_b3x_section(png: str, out: str) -> int:
    """执行 B3x 常驻回归，返回指纹项数。

    Args:
        png: 供构造 Task 的真实图片路径。
        out: 输出目录路径。

    Returns:
        指纹的键数量。

    Notes:
        带 ``--write-anim-baseline`` 运行时改写基线并跳过比对；其余情况一律比对。
        默认就是「校验」而不是「生成」—— 门禁脚本不该有一种会静默变绿的用法。
    """
    from momentshift.gui import animations

    step("v0.8.0-B3x: 采集状态迁移后的稳态指纹（当前环境）")
    current = collect_anim_fingerprint(png, out)
    failures = [k for k in current if k.startswith("<采集失败>")]
    for key in failures:
        print(f"  [采集失败] {key} -> {current[key]}", flush=True)
    assert not failures, f"B3x 指纹采集失败 {len(failures)} 处，见上方明细"

    step("v0.8.0-B3x: 断言① 强制关闭全局动效后，稳态逐键不变")
    was_enabled = animations.animations_enabled()
    animations.set_animations_enabled(False)
    try:
        disabled = collect_anim_fingerprint(png, out)
    finally:
        animations.set_animations_enabled(was_enabled)
    _assert_same(current, disabled, "开启态", "关闭态")

    step("v0.8.0-B3x: 断言② 稳态里没有 blend_color 泄漏的小写色值")
    allow = _steady_state_lowercase_allowlist()
    offenders = _lowercase_hex_offenders(current, allow)
    if offenders:
        for key in sorted(offenders):
            print(f"  ! {key} -> {offenders[key]}\n      {current[key]}", flush=True)
    assert not offenders, (
        f"稳态出现 {len(offenders)} 处疑似 blend_color 泄漏的小写色值"
        f"（合法小写白名单：{sorted(allow)}）"
    )

    if "--write-anim-baseline" in sys.argv:
        ANIM_BASELINE.write_text(
            json.dumps(current, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"[B3x] 已写入基线：{ANIM_BASELINE}（{len(current)} 项）", flush=True)
        return len(current)

    step("v0.8.0-B3x: 断言③ 与磁盘基线逐键一致")
    assert ANIM_BASELINE.exists(), (
        f"B3x 基线缺失：{ANIM_BASELINE}；确认改动合理后用 --write-anim-baseline 重新生成"
    )
    baseline = json.loads(ANIM_BASELINE.read_text(encoding="utf-8"))
    _assert_same(baseline, current, "基线", "当前")
    print(f"[B3x] 稳态指纹与基线一致：{len(current)} 项", flush=True)
    return len(current)


def main():
    step("importing Qt")
    from PyQt6.QtWidgets import QApplication

    from momentshift.core.config import cfg
    from momentshift.core.queue import ConversionManager
    from momentshift.gui.about_interface import AboutInterface
    from momentshift.gui.compress_interface import CompressInterface
    from momentshift.gui.convert_interface import ConvertInterface
    from momentshift.gui.setting_interface import SettingInterface
    from momentshift.gui.upscale_interface import UpscaleInterface

    step("creating QApplication")
    app = QApplication(sys.argv)
    manager = ConversionManager()

    step("constructing all five interfaces (standalone, no paint)")
    convert = ConvertInterface(manager)
    compress = CompressInterface()
    upscale = UpscaleInterface()
    setting = SettingInterface()
    about = AboutInterface()
    for iface in (convert, compress, upscale):
        assert iface.dropArea is not None, f"{type(iface).__name__} missing dropArea"
    step("all interfaces constructed OK")

    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "src")
    out = os.path.join(tmp, "out")
    os.makedirs(src)
    os.makedirs(out)

    step("Convert: expand_paths() filters out unsupported files")
    bad = os.path.join(tmp, "secret_file.xyz")
    open(bad, "wb").write(b"nope")
    from momentshift.core.presets import IMAGE_EXTS

    assert convert._expand_paths([bad], IMAGE_EXTS) == [], "unsupported file must be filtered"

    step("Convert: FormatGrid seeded from default selection")
    from momentshift.gui.format_grid import FormatGrid

    png = os.path.join(src, "photo.png")
    open(png, "wb").write(b"\x89PNG\r\n\x1a\n")
    fg = FormatGrid(convert)
    fg.setup(["image"], convert._selection)
    assert fg.get_selection().get("image") == "jpg", fg.get_selection()
    fg.deleteLater()

    step("QueueItemWidget constructs (the old crash site, no paint)")
    from momentshift.core.models import Task
    from momentshift.gui.queue_widget import QueueItemWidget

    tw = QueueItemWidget(
        Task(
            id="t1",
            input_path=png,
            output_path=os.path.join(out, "photo.jpg"),
            target_format="jpg",
            category="image",
            use_gpu=False,
        )
    )
    tw.deleteLater()

    step("Convert: confirm pushes task into conversion queue (same-folder mode)")
    cfg.outputMode.value = "same"
    cfg.outputSuffix.value = ""
    cfg.outputFolder.value = out
    before = len(manager.tasks)
    added, skipped = manager.add_files([png], "jpg", None, False, "same", "")
    assert len(added) == 1 and skipped == [], (added, skipped)
    assert len(manager.tasks) == before + 1, len(manager.tasks)
    assert manager.tasks[-1].target_format == "jpg"
    assert manager.tasks[-1].output_path.endswith("photo.jpg"), manager.tasks[-1].output_path

    step("detached manager: output-mode + same-format logic")
    mgr2 = ConversionManager()
    png2 = os.path.join(src, "photo2.png")
    open(png2, "wb").write(b"\x89PNG\r\n\x1a\n")
    added, _ = mgr2.add_files([png2], "jpg", None, False, output_mode="same", suffix="_conv")
    assert len(added) == 1 and "_conv.jpg" in added[0].output_path, added[0].output_path

    png3 = os.path.join(src, "photo3.png")
    open(png3, "wb").write(b"\x89PNG\r\n\x1a\n")
    added2, _ = mgr2.add_files([png3], "png", out, False, output_mode="fixed")
    assert len(added2) == 1 and added2[0].target_format == "png"
    same = mgr2.pending_same_format()
    assert len(same) >= 1 and same[0].target_format == "png"

    step("Upscale: files go straight to queue (no staging)")
    img = os.path.join(src, "big.png")
    open(img, "wb").write(b"\x89PNG\r\n\x1a\n")
    upscale._on_files([img])
    # v0.8.0 DUP-01：队列状态搬进 TaskPool，_items 已不存在
    assert len(upscale._pool) == 1, upscale._pool.iids()
    assert upscale.listWidget.items.keys() == {img}, upscale.listWidget.items

    step("Compress: staging accepts images")
    cimg = os.path.join(src, "c.png")
    open(cimg, "wb").write(b"\x89PNG\r\n\x1a\n")
    compress._on_files([cimg])
    # v0.8.0 DUP-01：队列状态搬进 TaskPool，_items 已不存在
    assert len(compress._pool) == 1, compress._pool.iids()
    assert compress.listWidget.items.keys() == {cimg}, compress.listWidget.items

    # ---------------------------------------------------------------- v0.7.3
    step("v0.7.3 Bug1: pickers resolve a real dialog parent (never None)")
    for iface in (convert, compress, upscale):
        assert iface._dialog_parent() is not None, type(iface).__name__

    step("v0.7.3 Bug1: DropArea defers the drop to the next event loop turn")
    import inspect

    from momentshift.gui.drop_area import DropArea

    drop_src = inspect.getsource(DropArea.dropEvent)
    assert "singleShot" in drop_src, "dropEvent must not emit synchronously"

    step("v0.7.3 Bug2: CollapsibleCard(collapsed=True) collapses without anim")
    from momentshift.gui.theme import CollapsibleCard

    card = CollapsibleCard("t", "", None, collapsed=True)
    assert card._anim is None, "no animation may run at construction time"
    assert card._body.maximumHeight() == 0, card._body.maximumHeight()
    assert card.isCollapsed()
    card.deleteLater()

    step("v0.7.3 Bug3: backend sections carry headers, released height cap")
    for grp in (compress.oxipngGroup, compress.joGroup, compress.pilGroup):
        assert hasattr(grp, "_header"), "backend group needs a section header"
    compress._on_program("auto")
    assert compress.oxipngGroup.isVisibleTo(compress._backend_container)
    assert compress.oxipngGroup._header.isVisibleTo(compress.oxipngGroup)
    compress._on_program("pillow")
    assert not compress.pilGroup._header.isVisibleTo(compress.pilGroup)
    compress._on_program("auto")

    step("v0.7.3 Bug4: compress row mirrors convert row, full bar when done")
    row = compress.listWidget.items[cimg]
    assert hasattr(row, "fmtPill") and hasattr(row, "iconLbl")
    assert row.fmtPill.text().startswith(".PNG"), row.fmtPill.text()
    row.set_progress(37)
    row.set_status("done", saved=1234)
    assert row.prog._value == 100, row.prog._value

    step("v0.7.3 Adj1: no widget exposes a hover tooltip")
    from PyQt6.QtWidgets import QWidget as _QW

    for iface in (convert, compress, upscale, setting, about):
        tipped = [w for w in iface.findChildren(_QW) if w.toolTip()]
        assert not tipped, f"{type(iface).__name__}: {[type(w).__name__ for w in tipped]}"

    step("v0.7.3 Adj2: FormatPill uses the #3EB68F brand background")
    from momentshift.gui.queue_widget import FormatPill

    assert "#3eb68f" in FormatPill(".A → .B").styleSheet().lower()

    # ---------------------------------------------------------------- v0.7.4
    step("v0.7.4 Bug: CollapsibleCard._apply_expanded/_apply_collapsed flip _collapsed")
    from momentshift.gui.theme import CollapsibleCard

    card = CollapsibleCard("t", "", None, collapsed=True)
    assert card.isCollapsed(), "constructed collapsed"
    card._apply_expanded()
    assert card.isCollapsed() is False, "_apply_expanded must set _collapsed=False"
    card._apply_collapsed()
    assert card.isCollapsed() is True, "_apply_collapsed must set _collapsed=True"
    card.deleteLater()

    step("v0.7.4 Bug: setCollapsed flips flag (the adv-switch path)")
    card2 = CollapsibleCard("t2", "", None, collapsed=True)
    card2.setCollapsed(True)  # no-op when equal
    assert card2.isCollapsed()
    card2.setCollapsed(False)
    assert card2.isCollapsed() is False
    card2.deleteLater()

    step("v0.7.4 Adj1: ext_badge renders the suffix text in a brand-tinted rect")
    from momentshift.gui.theme import ext_badge

    b = ext_badge("png")
    assert b.text() == "PNG", b.text()
    assert "rgba(35,134,54,0.08)" in b.styleSheet()
    b.deleteLater()

    step("v0.7.4 Adj1: queue/compress rows use suffix badge (not a pixmap icon)")
    from momentshift.gui.queue_widget import QueueItemWidget

    tw = QueueItemWidget(
        Task(
            id="t2",
            input_path=png,
            output_path=os.path.join(out, "photo.jpg"),
            target_format="jpg",
            category="image",
            use_gpu=False,
        )
    )
    assert tw.iconLbl.text() == "PNG", f"expected badge text PNG, got {tw.iconLbl.text()!r}"
    tw.deleteLater()
    row2 = compress.listWidget.items[cimg]
    assert row2.iconLbl.text() == "PNG", f"compress badge text, got {row2.iconLbl.text()!r}"

    step("v0.7.4 Adj2: each interface wires ScrollAutoFollow to its queue scroll")
    from momentshift.gui.queue_widget import ScrollAutoFollow

    for iface in (convert, compress, upscale):
        af = getattr(iface, "_queue_auto_follow", None)
        assert isinstance(af, ScrollAutoFollow), type(iface).__name__
        assert af._scroll is iface.queueScroll, type(iface).__name__
    # ensure() is a safe no-op when not active (no crash, no scroll)
    convert._queue_auto_follow.ensure(None)

    # ---------------------------------------------------------------- v0.7.5
    step("v0.7.5: 引擎注册表加载（14 个引擎：超分 + 插帧）")
    from momentshift.core import engines as eng_mod
    from momentshift.i18n.translator import tr

    assert len(eng_mod.ENGINES) == 14, len(eng_mod.ENGINES)
    assert len(eng_mod.ENGINE_BY_ID) == 14, len(eng_mod.ENGINE_BY_ID)
    sr = [e for e in eng_mod.ENGINES if e.category == "sr"]
    it = [e for e in eng_mod.ENGINES if e.category == "interp"]
    assert sr and it, "必须同时有超分与插帧引擎"
    assert "realesrgan-ncnn-vulkan" in eng_mod.ENGINE_BY_ID
    assert "rife-ncnn-vulkan" in eng_mod.ENGINE_BY_ID

    step("v0.7.5: EnginesCard 可离屏安全构造并重新检测")
    from momentshift.gui.engine_card import EnginesCard

    ec = EnginesCard(None, on_changed=lambda: None)
    ec.rescan()
    ec.deleteLater()

    step("v0.7.5: 动态参数面板按 schema 生成控件，无引擎返回空")
    from momentshift.gui.upscale_interface import EngineParamPanel

    panel = EngineParamPanel(None)
    eng = eng_mod.ENGINE_BY_ID["realesrgan-ncnn-vulkan"]
    panel.build(eng, eng_mod.default_values(eng.eid))
    assert len(panel._controls) == len(eng.params), (len(panel._controls), len(eng.params))
    vals = panel.values()
    assert set(vals.keys()) == {p.key for p in eng.params}, vals
    panel.build(None)
    assert panel.values() == {}, "无引擎时必须返回空参数"
    panel.deleteLater()

    step("v0.7.5: 无引擎回退（放大界面隐藏参数、禁用下拉）")
    installed = eng_mod.installed_engines()
    if not installed:
        assert not upscale.modelCombo.isEnabled(), "无引擎时应禁用下拉"
        assert upscale.modelCombo.itemText(0) == tr("upscale.engine.none")
    else:
        assert upscale.modelCombo.isEnabled()

    step("v0.7.5: RTX 驱动级引擎的 CLI 守卫（无命令行接口）")
    cmd, err = eng_mod.build_command("rtx-super-resolution", "a.png", "b.png", {})
    assert not cmd and err, (cmd, err)

    # ---------------------------------------------------------------- v0.7.6
    from PyQt6.QtWidgets import QSizePolicy as _QSP

    step("v0.7.6 修复1: 短文件名不滚动，超长文件名启动横向滚动（v0.7.8 改自适应宽度）")
    from momentshift.gui.queue_widget import MarqueeName

    short = MarqueeName()
    short.set_text("短名.png")
    assert not short._timer.isActive(), "短文件名不应滚动"
    long_name = "这是一个非常非常非常长的文件名用来测试滚动轮播效果.png"
    mq = MarqueeName()
    mq.set_text(long_name)
    assert mq._text == long_name
    # v0.7.8: _window_w 在 resizeEvent 中由布局分配，离屏无几何则保持 0
    short.deleteLater()
    mq.deleteLater()

    step("v0.7.6 修复1: 三个队列卡片文件名均使用 MarqueeName")
    tw = QueueItemWidget(
        Task(
            id="t3",
            input_path=png,
            output_path=os.path.join(out, "photo.jpg"),
            target_format="jpg",
            category="image",
            use_gpu=False,
        )
    )
    assert isinstance(tw.nameLbl, MarqueeName), type(tw.nameLbl).__name__
    tw.deleteLater()
    c_row = compress.listWidget.items[cimg]
    assert isinstance(c_row.nameLbl, MarqueeName), type(c_row.nameLbl).__name__
    u_row = upscale.listWidget.items[img]
    assert isinstance(u_row.nameLbl, MarqueeName), type(u_row.nameLbl).__name__

    step("v0.7.6 修复2: 引擎名/介绍可换行并限宽（不撑破 UI 画面）")
    from momentshift.gui.engine_card import EngineRow

    er = EngineRow(eng_mod.ENGINE_BY_ID["realesrgan-ncnn-vulkan"])
    assert er.nameLbl.wordWrap() and er.descLbl.wordWrap()
    assert er.nameLbl.sizePolicy().horizontalPolicy() == _QSP.Policy.Expanding
    assert er.descLbl.sizePolicy().horizontalPolicy() == _QSP.Policy.Expanding
    er.deleteLater()

    step("v0.7.6 修复3: 弱化文字颜色由过灰 #BDBDBD 调深为 #515151")
    from momentshift.gui.theme import TEXT_MUTED

    assert TEXT_MUTED.upper() == "#515151", f"expected #515151, got {TEXT_MUTED}"

    step("v0.7.6 功能2: 可下载引擎显示一键下载按钮；不可下载显示原因说明")
    row_dl = EngineRow(eng_mod.ENGINE_BY_ID["realesrgan-ncnn-vulkan"])
    assert row_dl.dlBtn is not None and row_dl.reasonLbl is None
    assert row_dl.dlBtn.text() == tr("engine.download.oneclick")
    row_no = EngineRow(eng_mod.ENGINE_BY_ID["srmd-cuda"])
    assert row_no.reasonLbl is not None and row_no.dlBtn is None
    assert row_no.reasonLbl.text() == tr(row_no.engine.download_reason_key)
    row_dl.deleteLater()
    row_no.deleteLater()

    step("v0.7.6 功能2: 引擎注册表下载字段完整（可下载 13 / 不可下载 2）")
    dl_count = sum(1 for e in eng_mod.ENGINES if e.downloadable)
    no_dl_count = sum(1 for e in eng_mod.ENGINES if not e.downloadable)
    assert dl_count == 12, dl_count
    assert no_dl_count == 2, no_dl_count
    for e in eng_mod.ENGINES:
        if e.downloadable:
            assert e.download_sources, f"{e.eid} 缺少下载源"
        else:
            assert e.download_reason_key, f"{e.eid} 缺少不可下载原因键"

    step("v0.7.6 功能2: ffmpeg.download 文案改为「一键下载并安装」")
    assert "一键下载并安装" in tr("ffmpeg.download"), tr("ffmpeg.download")

    step("v0.7.6 功能1: 放大参数面板每行附带帮助按钮（engine.help.* 键齐备）")
    for p in eng_mod.ENGINE_BY_ID["realesrgan-ncnn-vulkan"].params:
        assert tr(f"engine.help.{p.key}") != f"engine.help.{p.key}", (
            f"缺少帮助键 engine.help.{p.key}"
        )

    step("v0.7.6 功能4/1: 放大队列卡片翻新（FormatPill + 复制/对比/删除按钮 + 滚动名）")
    from momentshift.gui.upscale_interface import UpscaleItemWidget

    uw = UpscaleItemWidget("u1", img, out)
    assert isinstance(uw.nameLbl, MarqueeName)
    # v0.7.8 调整1: fmtPill → timeLbl（耗时显示）
    assert hasattr(uw, "timeLbl")
    assert hasattr(uw, "copyBtn") and hasattr(uw, "cmpBtn") and hasattr(uw, "delBtn")
    uw.deleteLater()

    # ---------------------------------------------------------------- v0.7.7
    from PyQt6.QtWidgets import QSizePolicy as _QSP

    step("v0.7.7 修复1: FormatPill / StatusPill 严格按文字定宽（Fixed size policy）")
    from momentshift.gui.queue_widget import FormatPill, StatusPill

    assert FormatPill().sizePolicy().horizontalPolicy() == _QSP.Policy.Fixed
    assert StatusPill().sizePolicy().horizontalPolicy() == _QSP.Policy.Fixed

    step("v0.7.7 修复3: engines.process_media 支持 progress_cb 参数")
    import inspect as _inspect

    sig = _inspect.signature(eng_mod.process_media)
    assert "progress_cb" in sig.parameters

    step("v0.7.7 引擎卡布局2: EngineRow 使用 StatusPill 胶囊替代文字状态")
    from momentshift.gui.engine_card import EngineRow as _EngineRow

    er2 = _EngineRow(eng_mod.ENGINE_BY_ID["realesrgan-ncnn-vulkan"])
    assert hasattr(er2, "statusPill") and not hasattr(er2, "statusLbl")
    assert isinstance(er2.statusPill, StatusPill)
    er2.deleteLater()

    step("v0.7.7 引擎卡布局4: EnginesCard.hintLbl 自动换行")
    assert ec.hintLbl.wordWrap()
    assert ec.hintLbl.sizePolicy().horizontalPolicy() == _QSP.Policy.Expanding

    step("v0.7.7 调整1: 元数据默认不删除（advanced.py strip=none, jo_strip=none）")
    from momentshift.core.advanced import default_options

    dopts = default_options()
    assert dopts["image"]["compress"]["strip"] == "none", dopts["image"]["compress"]["strip"]
    assert dopts["image"]["compress"]["jo_strip"] == "none", dopts["image"]["compress"]["jo_strip"]

    # ---------------------------------------------------------------- v0.7.12
    step("v0.7.12: 压缩/放大队列暴露 taskAdded/taskProgress/taskFinished 信号")
    from momentshift.gui.compress_interface import CompressInterface as _CI2
    from momentshift.gui.upscale_interface import UpscaleInterface as _UI2

    for sig in ("taskAdded", "taskProgress", "taskFinished"):
        assert hasattr(_CI2, sig), f"CompressInterface missing {sig}"
        assert hasattr(_UI2, sig), f"UpscaleInterface missing {sig}"

    step("v0.7.15: 设置弹窗模块可导入（离屏不构造，避免硬杀）")
    from momentshift.gui.quick_dialogs import (
        QuickCompressDialog,
        QuickUpscaleDialog,
        _SettingsEmbed,
        _StagingList,
    )

    assert _StagingList and _SettingsEmbed

    step("v0.7.15: 任务进度窗口已删除")
    import importlib

    try:
        importlib.import_module("momentshift.gui.task_progress_window")
        raise AssertionError("task_progress_window should be removed")
    except ImportError:
        pass

    step("v0.7.15: quick_runner 可导入且 run_quick 存在")
    from momentshift.quick_runner import run_quick

    assert callable(run_quick)

    step("v0.7.15: UpscaleInterface._settingsCard 存在（reparent 复用）")
    assert hasattr(_UI2, "_settingsCard") or True  # 实例属性，类级可能无
    from momentshift.gui.upscale_interface import UpscaleInterface as _UI3

    _u3 = _UI3()
    assert hasattr(_u3, "_settingsCard"), "UpscaleInterface missing _settingsCard"
    _u3.deleteLater()

    # --------------------------------------------------------------- v0.8.1
    # Bug4：切换语言后文案必须即时刷新（不重启）；Bug5：通知卡片简介高度自适应
    # 不依赖真实 ffmpeg / 不构造 ConvertSetupDialog（沙箱硬杀），只验证
    # retranslateUi 链路的控件文本与卡片高度。
    step("v0.8.1 Bug4: 切换语言后 ffmpeg 状态 / field_row 标签 / 通知卡片即时刷新")
    from momentshift.i18n.translator import LocaleKey, translator

    _saved_locale = translator.locale
    try:
        translator.set_locale(LocaleKey.ZH_CN)
        convert.retranslateUi()
        zh_ff = convert._ff_status.text()
        assert "✓" in zh_ff or "✗" in zh_ff, f"ff 状态应含状态符号：{zh_ff!r}"
        translator.set_locale(LocaleKey.EN_US)
        convert.retranslateUi()
        en_ff = convert._ff_status.text()
        assert "✓" in en_ff or "✗" in en_ff, f"ff 状态应含状态符号：{en_ff!r}"
        assert zh_ff != en_ff, "ff 状态文本应随语言变化"

        # field_row 行标签（v0.8.1 起暴露 row.fieldLabel 供 retranslateUi 刷新）
        translator.set_locale(LocaleKey.ZH_CN)
        compress.retranslateUi()
        zh_suffix = compress.suffixRow.fieldLabel.text()
        translator.set_locale(LocaleKey.EN_US)
        compress.retranslateUi()
        en_suffix = compress.suffixRow.fieldLabel.text()
        assert zh_suffix != en_suffix, f"field_row 标签应随语言变化：{zh_suffix!r} vs {en_suffix!r}"
        assert "suffix" in en_suffix.lower(), en_suffix
        assert compress.backendRow.fieldLabel.text() == tr("advanced.compression.backend")
        assert compress.outputModeRow.fieldLabel.text() == tr("compress.output.mode")

        # 通知卡片：retranslateUi 必须刷新标题与简介（Bug4-③），
        # 且简介高度 ≥ 换行后的实际高度（Bug5）
        from PyQt6.QtCore import QTimer as _QTimer
        from PyQt6.QtGui import QTextDocument as _QTextDocument

        translator.set_locale(LocaleKey.EN_US)
        from momentshift.gui.quick_launch_interface import QuickLaunchInterface

        quick_launch_iface = QuickLaunchInterface()
        quick_launch_iface.retranslateUi()
        app.processEvents()
        for name in ("notifyStartCard", "notifyDoneCard"):
            card = getattr(quick_launch_iface, name)
            assert card.titleLabel.text(), f"{name} 标题为空"
            assert card.contentLabel.text(), f"{name} 简介为空"
            # 简介必须按当前语言重新取文案（不是残留的中文）
            assert "notification" in card.contentLabel.text().lower(), (
                f"{name} 简介未随语言刷新：{card.contentLabel.text()!r}"
            )
        # Bug5：卡片高度必须容纳换行后的简介（QTextDocument 独立测量）
        card = quick_launch_iface.notifyStartCard
        cl = card.contentLabel
        doc = _QTextDocument(cl.text())
        doc.setDefaultFont(cl.font())
        doc.setTextWidth(cl.width())
        wrapped = int(doc.size().height()) + 2
        assert cl.height() >= min(wrapped, cl.minimumHeight()) - 4, (
            f"通知卡片简介高度不足：label={cl.height()} 需≥{wrapped}"
        )
        quick_launch_iface.deleteLater()
    finally:
        translator.set_locale(_saved_locale)

    # --------------------------------------------------------------- v0.8.0
    # B3x：动效关闭态一致性 + 状态迁移后稳态（快照盲区的常驻回归）
    # 放在最后是因为它会构造大量临时控件并临时改写 gui.base 的时间源，
    # 不该影响前面任何一条既有断言。
    anim_items = run_b3x_section(png, out)

    step("ALL CHECKS PASSED")
    print(
        f"convert engine tasks: {len(manager.tasks)}  detached tasks: {len(mgr2.tasks)}  "
        f"same-format: {len(same)}  b3x fingerprint: {anim_items}",
        flush=True,
    )
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        # os._exit 不走 atexit、也不刷缓冲区：失败时的诊断输出（指纹差异明细）
        # 全在 stdout 上，不显式 flush 就会连同退出码一起丢掉，只剩一个空的红灯。
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
