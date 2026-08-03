"""v0.8.0 B4 行为等价性回归 —— ``quick_runner._ensure_batch_notify`` 合并等价性。

B4 把转换 / 压缩 / 放大三条「整批完成弹一次通知」的流水线（原本三份逐字重复、
各自演化的实现）合并成 ``_ensure_batch_notify(window, holder, added_signal,
finished_signal, done_msg_key, label)``。

本文件证明合并后**行为等价**，且 B4 引入的 ``*_a`` 变长槽在结构上杜绝了
「改了信号签名 → 槽静默失联」这类回归：

1. **绑定幂等**：同一 holder 多次调用只接一次信号（不重复连接 / 不重复通知）。
2. **各 holder 计数独立**：转换(manager) / 压缩(iface) / 放大(iface) 互不串台。
3. **变长槽真实接得住各信号的实际元数**：
   - 转换 ``task_added=Signal(object)`` / ``task_finished=Signal(str,bool,str)``；
   - 压/放 ``taskAdded=Signal(str,str)`` / ``taskFinished=Signal(str,str)``。
   用真实 pyqt 信号实例连线，触发时把实际参数透传给 ``*_a`` 槽，验证不会因
   元数不匹配而静默失联。
4. **触发时机与改造前逐字一致**：added 计数到 n、finished 到 n 且未通知过时，
   恰好弹一次；之后再来一批（n 再次上涨 → notified 重置）会重新武装并再弹一次。
5. **结构性防御**：槽一律 ``*_a``，无论信号日后改成几参数都不会再「连上却从不触发」。

不构造 QApplication，仅用 ``QCoreApplication``（与 test_task_pool 同制式）。
``_notify`` 与 ``QTimer.singleShot`` 由 autouse 夹具统一接管：前者换成本地记录器，
绝不弹真实系统托盘；后者改成同步执行（headless 下 singleShot(0) 不会自动触发）。

注意：记录器必须**只有一份**。早期版本让每个 ``_bind`` 各自 monkeypatch 一次
``qr._notify``，后一次 patch 会静默覆盖前一次，导致「先绑定的那条流水线永远记不到
通知」的假阴性。现改为模块级单一 spy + 共享列表。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QCoreApplication

from momentshift import quick_runner as qr
from momentshift.core.qt_compat import QObject, Signal
from momentshift.i18n.translator import tr


@pytest.fixture(scope="module")
def qapp():
    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([])
    return app


# ---------------------------------------------------------------------------
# 统一的通知记录器：整个模块只有这一份，避免多次 patch 互相覆盖
# ---------------------------------------------------------------------------
_NOTIFY_CALLS: list[tuple[str, str]] = []


def _notify_spy(window, title, body, enabled_key="quickNotifyDone"):
    """替身：把「本应弹出的通知」留档，不碰真实托盘。"""
    _NOTIFY_CALLS.append((body, enabled_key))


@pytest.fixture(autouse=True)
def _patch_notify_and_timer():
    """接管 ``_notify`` 与 ``QTimer.singleShot``。

    - ``_notify``：换成本地 spy。window=None 时真实实现会去摸托盘，headless 下崩。
    - ``singleShot``：headless 下 ``singleShot(0, cb)`` 在 ``processEvents()`` 里
      不保证触发，这里改成同步执行，保证「整批完成 → 弹通知」的延迟回调跑得到。
    """
    saved_notify = qr._notify
    saved_single = qr.QTimer.singleShot
    qr._notify = _notify_spy
    qr.QTimer.singleShot = staticmethod(lambda _ms, cb: cb())
    _NOTIFY_CALLS.clear()
    yield
    qr.QTimer.singleShot = saved_single
    qr._notify = saved_notify
    _NOTIFY_CALLS.clear()


class _Holder(QObject):
    """压缩 / 放大流水线的界面替身：信号元数与真实界面一致（2 参数）。"""

    taskAdded = Signal(str, str)
    taskFinished = Signal(str, str)


class _Manager(QObject):
    """转换流水线的 manager 替身：信号元数与 ConversionManager 一致。

    ``task_finished`` 是 **3 参数** —— 改造前转换那份曾写成 4 参数槽，PyQt 静默
    连接失败，通知从来没弹出来过。这里用真实元数把回归钉死。
    """

    task_added = Signal(object)
    task_finished = Signal(str, bool, str)


CONVERT_KEY = "quick.notify.convert_done"
COMPRESS_KEY = "quick.notify.compress_done"
UPSCALE_KEY = "quick.notify.upscale_done"


def _bind(holder, added, finished, done_key, label):
    """接上流水线，返回共享的通知记录列表。"""
    qr._ensure_batch_notify(None, holder, added, finished, done_key, label)
    return _NOTIFY_CALLS


def _bodies_for(key: str) -> list[str]:
    """记录中属于该流水线（按翻译后文案匹配）的通知条数。"""
    want = tr(key)
    return [b for b, _k in _NOTIFY_CALLS if b == want]


# ---------------------------------------------------------------------------
# 1) 绑定幂等
# ---------------------------------------------------------------------------
def test_bind_is_idempotent_per_holder(qapp):
    """同一 holder 反复绑定只生效一次：不重复连信号，也不重复弹通知。"""
    ci = _Holder()
    _bind(ci, ci.taskAdded, ci.taskFinished, COMPRESS_KEY, "compress")
    assert getattr(ci, qr._NOTIFY_BOUND_ATTR) is True

    # 再绑三次：应直接 return（标记已在），不新增连接
    for _ in range(3):
        qr._ensure_batch_notify(None, ci, ci.taskAdded, ci.taskFinished, COMPRESS_KEY, "compress")
    assert getattr(ci, qr._NOTIFY_BOUND_ATTR) is True

    ci.taskAdded.emit("i1", "a.png")
    ci.taskFinished.emit("i1", "done")
    assert len(_NOTIFY_CALLS) == 1, "重复绑定不应造成重复通知"


def test_bound_attr_name_is_stable():
    """标记属性名是跨模块约定（quick_runner 与界面共用），不允许悄悄改名。"""
    assert qr._NOTIFY_BOUND_ATTR == "_quick_notify_bound"


# ---------------------------------------------------------------------------
# 2) 各 holder 计数独立（合并后最容易踩的串台点）
# ---------------------------------------------------------------------------
def test_convert_compress_upscale_counters_are_independent(qapp):
    """三条流水线各自持有独立 state：一条跑完不会替另外两条弹通知。"""
    mgr = _Manager()
    ci = _Holder()
    ui = _Holder()
    _bind(mgr, mgr.task_added, mgr.task_finished, CONVERT_KEY, "convert")
    _bind(ci, ci.taskAdded, ci.taskFinished, COMPRESS_KEY, "compress")
    _bind(ui, ui.taskAdded, ui.taskFinished, UPSCALE_KEY, "upscale")

    # 只有转换批跑完
    mgr.task_added.emit(object())
    mgr.task_finished.emit("t", True, "")

    assert len(_bodies_for(CONVERT_KEY)) == 1
    assert len(_bodies_for(COMPRESS_KEY)) == 0
    assert len(_bodies_for(UPSCALE_KEY)) == 0

    # 再让压缩批跑完，转换不应被二次触发
    ci.taskAdded.emit("i1", "a.png")
    ci.taskFinished.emit("i1", "done")
    assert len(_bodies_for(CONVERT_KEY)) == 1
    assert len(_bodies_for(COMPRESS_KEY)) == 1
    assert len(_bodies_for(UPSCALE_KEY)) == 0


# ---------------------------------------------------------------------------
# 3) 变长槽真实接得住各信号的实际元数
# ---------------------------------------------------------------------------
def test_varargs_slot_accepts_convert_3arg_signal(qapp):
    """转换 ``task_finished`` 是 3 参数信号 —— 历史 bug 正是槽元数写错导致失联。"""
    mgr = _Manager()
    _bind(mgr, mgr.task_added, mgr.task_finished, CONVERT_KEY, "convert")
    mgr.task_added.emit(object())
    mgr.task_finished.emit("tid", True, "some error text")
    assert len(_bodies_for(CONVERT_KEY)) == 1, "3 参数信号必须能触发合并后的 *_a 槽"


def test_varargs_slot_accepts_iface_2arg_signal(qapp):
    """压/放 ``taskAdded``/``taskFinished`` 是 2 参数信号，同样必须触发。"""
    ui = _Holder()
    _bind(ui, ui.taskAdded, ui.taskFinished, UPSCALE_KEY, "upscale")
    ui.taskAdded.emit("i1", "a.png")
    ui.taskFinished.emit("i1", "done")
    assert len(_bodies_for(UPSCALE_KEY)) == 1


def test_varargs_slot_accepts_zero_arg_signal(qapp):
    """结构性防御：日后信号改成 0 参数也不会失联（旧写法必炸）。"""

    class _Zero(QObject):
        a = Signal()
        f = Signal()

    z = _Zero()
    _bind(z, z.a, z.f, COMPRESS_KEY, "compress")
    z.a.emit()
    z.f.emit()
    assert len(_bodies_for(COMPRESS_KEY)) == 1


def test_varargs_slot_accepts_four_arg_signal(qapp):
    """结构性防御：日后信号扩到 4 参数同样接得住。"""

    class _Four(QObject):
        a = Signal(str, str, int, bool)
        f = Signal(str, str, int, bool)

    o = _Four()
    _bind(o, o.a, o.f, UPSCALE_KEY, "upscale")
    o.a.emit("x", "y", 1, True)
    o.f.emit("x", "y", 1, True)
    assert len(_bodies_for(UPSCALE_KEY)) == 1


# ---------------------------------------------------------------------------
# 4) 触发时机与改造前逐字一致
# ---------------------------------------------------------------------------
_PIPELINES = [
    ("convert", CONVERT_KEY, _Manager, "task_added", "task_finished"),
    ("compress", COMPRESS_KEY, _Holder, "taskAdded", "taskFinished"),
    ("upscale", UPSCALE_KEY, _Holder, "taskAdded", "taskFinished"),
]


def _emit_added(holder, label, attr, i):
    sig = getattr(holder, attr)
    if label == "convert":
        sig.emit(object())
    else:
        sig.emit(f"i{i}", f"f{i}.png")


def _emit_finished(holder, label, attr, i):
    sig = getattr(holder, attr)
    if label == "convert":
        sig.emit(f"t{i}", True, "")
    else:
        sig.emit(f"i{i}", "done")


@pytest.mark.parametrize("label,key,cls,added_attr,fin_attr", _PIPELINES)
def test_notify_fires_once_when_batch_done_then_rearms(qapp, label, key, cls, added_attr, fin_attr):
    """整批完成恰好弹一次；下一批入队会重置 notified，完成后再弹一次。"""
    holder = cls()
    _bind(holder, getattr(holder, added_attr), getattr(holder, fin_attr), key, label)

    # 第一批：2 入 2 完 → 恰好一次
    for i in range(2):
        _emit_added(holder, label, added_attr, i)
    for i in range(2):
        _emit_finished(holder, label, fin_attr, i)
    assert len(_bodies_for(key)) == 1, f"{label}: 整批完成应只弹一次"

    # 第二批：再入 1 再完 1 → 重新武装并再弹一次
    _emit_added(holder, label, added_attr, 9)
    _emit_finished(holder, label, fin_attr, 9)
    assert len(_bodies_for(key)) == 2, f"{label}: 下一批完成应再次通知"


@pytest.mark.parametrize("label,key,cls,added_attr,fin_attr", _PIPELINES)
def test_no_notify_until_all_finished(qapp, label, key, cls, added_attr, fin_attr):
    """入队 2 个只完成 1 个 → 不通知（与改造前逐字一致）。"""
    holder = cls()
    _bind(holder, getattr(holder, added_attr), getattr(holder, fin_attr), key, label)
    for i in range(2):
        _emit_added(holder, label, added_attr, i)
    _emit_finished(holder, label, fin_attr, 0)
    assert len(_bodies_for(key)) == 0

    # 补上最后一个才通知
    _emit_finished(holder, label, fin_attr, 1)
    assert len(_bodies_for(key)) == 1


@pytest.mark.parametrize("label,key,cls,added_attr,fin_attr", _PIPELINES)
def test_finished_before_any_added_does_not_notify(qapp, label, key, cls, added_attr, fin_attr):
    """n==0 时 finished 先到（乱序信号）不应误弹 —— 守住 ``state["n"] > 0``。"""
    holder = cls()
    _bind(holder, getattr(holder, added_attr), getattr(holder, fin_attr), key, label)
    _emit_finished(holder, label, fin_attr, 0)
    assert len(_bodies_for(key)) == 0


@pytest.mark.parametrize("label,key,cls,added_attr,fin_attr", _PIPELINES)
def test_extra_finished_after_batch_does_not_double_notify(
    qapp, label, key, cls, added_attr, fin_attr
):
    """整批完成后又飘来一个 finished（重试/重复信号）不应重复弹。"""
    holder = cls()
    _bind(holder, getattr(holder, added_attr), getattr(holder, fin_attr), key, label)
    _emit_added(holder, label, added_attr, 0)
    _emit_finished(holder, label, fin_attr, 0)
    assert len(_bodies_for(key)) == 1
    for i in range(3):
        _emit_finished(holder, label, fin_attr, i)
    assert len(_bodies_for(key)) == 1, "notified 未被重置前不应二次通知"


# ---------------------------------------------------------------------------
# 5) 结构性防御：所有调用点用的键都真实存在于 i18n
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key", [CONVERT_KEY, COMPRESS_KEY, UPSCALE_KEY, "quick.notify.title", "quick.notify.started"]
)
def test_notify_keys_are_translated(key):
    """通知文案键必须有译文；缺失时 tr 会回吐原键，这里直接判死。"""
    text = tr(key)
    assert text and text != key, f"通知键未翻译：{key}"
