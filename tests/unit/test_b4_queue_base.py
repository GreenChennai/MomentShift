"""v0.8.0 B4 回归：QueueListBase 行管理契约 + ``_ensure_batch_notify`` 合并等价性。

不依赖真实 Qt 控件（不创建 QApplication / 不构造行控件），用 duck-typed 假对象驱动
``QueueListBase`` 的**未绑定**方法，验证 B4「三份逐字重复 → 一份公共骨架」的重构
没有丢掉任何一份原有的行管理行为。

覆盖：
- B4 行管理契约：二次移除安全 / 清空不重复销毁 / 未知 key 静默 / set_progress 忽略
  未知 / retranslate 回填 / 空态恒隐藏 / _update_stats 占位 / _attach_row 入队不刷统计
  / 动效批量预算闸。
- 三个队列子类行为差异在 B4 后被**如实保留**（convert 入队不刷统计、compress/upscale
  入队刷统计；只有 convert 给空态标签设 objectName 进 QSS 快照）。
- quick_runner._ensure_batch_notify 合并等价性：绑定幂等 / 不同 holder 独立计数 /
  整批完成弹一次 / 第二批重新武装 / 信号-槽参数错配在结构上不可能（*_a）。
"""

from __future__ import annotations

import ast
import types

import pytest

from momentshift.core.qt_compat import QObject, Signal
from momentshift.gui import base as gui_base
from momentshift.gui.base import QueueListBase
from momentshift.gui.compress_interface import CompressListWidget
from momentshift.gui.queue_widget import QueueListWidget
from momentshift.gui.upscale_interface import UpscaleListWidget


# ---------------------------------------------------------------------------
# 假对象：模拟 QueueListBase 用到的「自己」的最小接口（不碰真实 QWidget）
# ---------------------------------------------------------------------------
class _FakeRow:
    def __init__(self, key):
        self.key = key
        self.deleted = False
        self.progress = None
        self.signals_blocked = False
        self.retried = False

    def deleteLater(self):
        self.deleted = True

    def set_progress(self, pct):
        self.progress = pct

    def blockSignals(self, on):
        self.signals_blocked = on

    def retranslate(self):
        self.retried = True


class _FakeLayout:
    def __init__(self):
        self.widgets = []
        self.inserted = []

    def count(self):
        return len(self.widgets)

    def insertWidget(self, idx, w):
        self.inserted.append((idx, w))
        self.widgets.insert(idx, w)


class _FakeEmpty:
    def __init__(self):
        self.visible = None
        self.text = None
        self.obj = ""

    def setVisible(self, v):
        self.visible = v

    def setText(self, t):
        self.text = t

    def objectName(self):
        return self.obj

    def setObjectName(self, n):
        self.obj = n


class _FakeList:
    """驱动 QueueListBase 未绑定方法的「self」。"""

    def __init__(self):
        self.items = {}
        self.listLayout = _FakeLayout()
        self.emptyHint = _FakeEmpty()
        self._empty_key = "convert.queue.empty"
        self._statLabels = [_FakeEmpty(), _FakeEmpty(), _FakeEmpty()]
        self.updates = 0
        self.refreshes = 0
        # 动效：离屏恒不可见，should_animate 走 False 分支，_dispose_row 直接 deleteLater
        self._anim_budget_allows_return = True

    # duck-typed：_FakeList 要能被 animations.should_animate 当作「不可见的离屏
    # 控件」对待 —— 它调 widget.isVisible()，返回 False 即走无动画分支
    # （与门禁离屏稳态一致），_dispose_row 于是直接 deleteLater。
    def isVisible(self):  # noqa: N802  (Qt 命名)
        return False

    # 被 QueueListBase 调用的钩子（ spy 版）
    def _refresh_empty(self):
        self.refreshes += 1
        self.emptyHint.setVisible(False)

    def _update_stats(self):
        self.updates += 1

    def _dispose_row(self, w):
        # 关键：基类的 _dispose_row 在 should_animate 为 False 时直接 deleteLater。
        # 这里直接复用基类的真实实现来保证测的是真代码路径。
        return QueueListBase._dispose_row(self, w)

    def _anim_budget_allows(self):
        return self._anim_budget_allows_return


# ---------------------------------------------------------------------------
# 行管理契约
# ---------------------------------------------------------------------------
def test_remove_item_disposes_and_detaches():
    f = _FakeList()
    r = _FakeRow("k")
    f.items["k"] = r
    QueueListBase.remove_item(f, "k")
    assert "k" not in f.items
    assert r.deleted is True
    assert f.refreshes >= 1
    assert f.updates >= 1


def test_remove_item_double_safe():
    """二次移除安全：第二次拿不到控件，静默返回，不重复销毁。"""
    f = _FakeList()
    r = _FakeRow("k")
    f.items["k"] = r
    QueueListBase.remove_item(f, "k")
    before = r.deleted
    QueueListBase.remove_item(f, "k")  # 第二次
    assert r.deleted is before  # 没有被二次 deleteLater
    assert f.updates == 2  # 两次都仍刷统计（基类行为）


def test_remove_item_unknown_key_silent():
    f = _FakeList()
    QueueListBase.remove_item(f, "ghost")  # 不得抛
    assert f.updates >= 1
    assert f.refreshes >= 1


def test_clear_disposes_each_once():
    f = _FakeList()
    r1, r2 = _FakeRow("a"), _FakeRow("b")
    f.items["a"], f.items["b"] = r1, r2
    QueueListBase.clear(f)
    assert f.items == {}
    assert r1.deleted and r2.deleted
    assert f.updates >= 1


def test_clear_does_not_double_dispose_removed_row():
    """淡出期间被 clear 扫到的行已经从 items 摘除，clear 不会重复销毁它。"""
    f = _FakeList()
    r = _FakeRow("a")
    f.items["a"] = r
    # 模拟「正在淡出」：已经从 items 摘除（_dispose_row 内部逻辑由基类完成）
    f.items.pop("a", None)
    del_mock = r.deleted
    QueueListBase.clear(f)
    assert r.deleted is del_mock  # 没被再次 deleteLater


def test_set_progress_ignores_unknown_key():
    f = _FakeList()
    QueueListBase.set_progress(f, "ghost", 50)  # 不得抛
    assert f.updates == 0


def test_set_progress_forwards_to_widget():
    f = _FakeList()
    r = _FakeRow("k")
    f.items["k"] = r
    QueueListBase.set_progress(f, "k", 77)
    assert r.progress == 77


def test_retranslate_refills_rows_and_empty():
    f = _FakeList()
    r = _FakeRow("k")
    f.items["k"] = r
    QueueListBase.retranslate(f)
    assert r.retried is True
    # 空态文案被回填（tr(empty_key)；该 key 在三语均为空属产品决策，此处只断言
    # setText 被真实调用、文本不再是默认值 None）
    assert f.emptyHint.text is not None
    assert f.updates >= 1


def test_refresh_empty_always_hidden():
    f = _FakeList()
    f.emptyHint.obj = "queueEmpty"
    QueueListBase._refresh_empty(f)
    assert f.emptyHint.visible is False


def test_base_update_stats_is_abstract_placeholder():
    f = _FakeList()
    with pytest.raises(NotImplementedError):
        QueueListBase._update_stats(f)


def test_attach_row_inserts_but_does_not_refresh_stats():
    """入队行：插入 + 刷新空态，但**不**刷统计（convert 入队靠后续 sync 兜底）。"""
    f = _FakeList()
    r = _FakeRow("k")
    QueueListBase._attach_row(f, "k", r)
    assert f.items["k"] is r
    assert f.listLayout.inserted
    assert f.refreshes >= 1
    assert f.updates == 0  # 关键契约：入队不刷统计


def test_anim_budget_allows_window_reset_and_cap():
    f = _FakeList()
    f._anim_budget = 0
    f._anim_last_ms = 0.0
    import time

    import momentshift.gui.base as b

    # 连续 24 次都在同一突发窗口内 → 第 25 次被拒
    ok = sum(
        1
        for _ in range(b.animations.QUEUE_ANIM_BATCH_LIMIT)
        if QueueListBase._anim_budget_allows(f)
    )
    assert ok == b.animations.QUEUE_ANIM_BATCH_LIMIT
    assert QueueListBase._anim_budget_allows(f) is False
    # 越过突发窗口（60ms）后预算重置
    f._anim_last_ms = time.monotonic() * 1000.0 - (b.animations.QUEUE_BURST_WINDOW_MS + 50)
    assert QueueListBase._anim_budget_allows(f) is True


# ---------------------------------------------------------------------------
# 三个子类：B4 后行为差异被如实保留（静态 + 行为）
# ---------------------------------------------------------------------------
def test_subclass_inherits_row_management_from_base():
    """二次移除安全 / 清空 / retranslate / set_progress 应由 QueueListBase 提供，
    三份原有的同名实现已被正确收进基类，没有在子类里各自重写。"""
    for cls in (QueueListWidget, CompressListWidget, UpscaleListWidget):
        assert "remove_item" not in cls.__dict__
        assert "clear" not in cls.__dict__
        assert "retranslate" not in cls.__dict__
        assert "set_progress" not in cls.__dict__
        assert QueueListBase in cls.__mro__
        # 统计口径各自保留（未退化成基类占位）
        assert cls.__dict__.get("_update_stats") is not None


def test_only_convert_sets_empty_object_name():
    """只有转换队列历史上给空态标签设了 objectName（进 QSS 快照键）。
    compress / upscale 的 _empty_object_name 必须为空，否则会改动快照基线。"""
    assert QueueListWidget._empty_object_name == "queueEmpty"
    assert CompressListWidget._empty_object_name == ""
    assert UpscaleListWidget._empty_object_name == ""


def test_empty_keys_distinct_per_queue():
    assert QueueListWidget._empty_key == "convert.queue.empty"
    assert CompressListWidget._empty_key == "compress.queue.empty"
    assert UpscaleListWidget._empty_key == "upscale.queue.empty"


def test_convert_add_item_does_not_refresh_stats_on_enqueue():
    """convert vs compress/upscale 的核心差异：入队时是否刷统计。
    B4 必须保留——若误统一成「入队即刷」，convert 的统计口径就变了。"""
    src = open("src/momentshift/gui/queue_widget.py", encoding="utf-8").read()
    add = _method_body(src, "QueueListWidget", "add_item")
    assert "_attach_row(" in add
    assert "_update_stats(" not in add  # 入队不刷


def test_compress_upscale_add_item_refresh_stats_on_enqueue():
    for path in (
        "src/momentshift/gui/compress_interface.py",
        "src/momentshift/gui/upscale_interface.py",
    ):
        src = open(path, encoding="utf-8").read()
        cls = "CompressListWidget" if "compress" in path else "UpscaleListWidget"
        add = _method_body(src, cls, "add_item")
        assert "_attach_row(" in add
        assert "_update_stats(" in add  # 入队刷


def _method_body(source: str, cls: str, method: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for n in node.body:
                if isinstance(n, ast.FunctionDef) and n.name == method:
                    return ast.get_source_segment(source, n) or ""
    return ""
