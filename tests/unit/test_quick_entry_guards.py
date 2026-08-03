"""v0.8.0 门禁盲区补测：两个入口（主窗口 / 右键快速调用）的异常兜底一致性。

MomentShift 有两条进程入口：

- ``__main__.main()`` → ``create_application(sys.argv, app_cls=Application)``；
  ``Application`` 覆写了 ``notify()``，槽函数里抛出的任何异常都会被兜住写日志，
  程序不闪退。
- ``quick_runner._setup_app()`` → ``create_application(sys.argv, quick_mode=True)``，
  **没有传 app_cls** → 用的是裸 ``QApplication``。

PyQt6 下，槽函数里未捕获的 Python 异常会在调用 ``sys.excepthook`` 之后触发
``qFatal()``/``abort()``。也就是说同一个 bug（例如输出目录指向已断开的盘符）：
主窗口路径只是「点了没反应」，右键快速调用路径直接闪退。

这一层差异六项门禁全都看不见（ruff 看语法、pytest 不起进程、offscreen_smoke 只
导入 quick_runner 不构造 app、qss/config/i18n 更无关）。本文件用静态 AST 把这条
约定钉住：任何人日后给 quick_runner 补上兜底，xfail 会转 XPASS 提醒回收。

纯静态解析，不构造 QApplication，不起子进程。
"""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest

from momentshift import __main__ as entry
from momentshift import quick_runner as qr
from momentshift.app_bootstrap import create_application


def _call_kwargs(func, callee_name: str) -> list[set[str]]:
    """收集 func 源码里对 callee_name 的所有调用用到的关键字参数名。"""
    tree = ast.parse(inspect.getsource(func).lstrip())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name == callee_name:
                found.append({kw.arg for kw in node.keywords if kw.arg})
    return found


def test_main_window_entry_installs_exception_guard():
    """主窗口入口必须显式传入带 notify 兜底的 Application 子类。"""
    kwargs = _call_kwargs(entry.main, "create_application")
    assert kwargs, "__main__.main 未调用 create_application"
    assert any("app_cls" in k for k in kwargs), (
        "主窗口入口丢了 app_cls=Application —— 槽内异常将不再被兜住"
    )


def test_application_subclass_overrides_notify():
    """兜底的实现方式不能被悄悄换掉：Application 必须自己覆写 notify。"""
    assert "notify" in vars(entry.Application), "Application 不再覆写 notify，异常兜底失效"


def test_create_application_default_is_bare_qapplication():
    """默认不传 app_cls 时用的是裸 QApplication —— 这正是快速调用的现状。"""
    sig = inspect.signature(create_application)
    assert sig.parameters["app_cls"].default is None


@pytest.mark.xfail(
    reason="已知缺陷（预存在，非 B4 引入）：quick_runner._setup_app 调用 "
    "create_application 时没有传 app_cls=Application，右键快速调用全程跑在裸 "
    "QApplication 上。PyQt6 下槽内未捕获异常会在 sys.excepthook 之后 abort()，"
    "于是同一个 bug 在主窗口只是「点了没反应」，在右键快速调用会直接闪退。"
    "修复方式：_setup_app 里传入 __main__.Application（或把该类下沉到 "
    "app_bootstrap 供两处共用）。",
    strict=True,
)
def test_quick_entry_installs_exception_guard():
    kwargs = _call_kwargs(qr._setup_app, "create_application")
    assert kwargs, "quick_runner._setup_app 未调用 create_application"
    assert any("app_cls" in k for k in kwargs)


def test_quick_entry_sets_quick_mode():
    """反向确认：quick_mode=True 这条是在的（别把上面的 xfail 误读成整个调用有问题）。"""
    kwargs = _call_kwargs(qr._setup_app, "create_application")
    assert any("quick_mode" in k for k in kwargs)


# ---------------------------------------------------------------------------
# connect_autosave 的作用域边界（v0.8.0 ODD-22 的隐性前提）
# ---------------------------------------------------------------------------
def test_connect_autosave_only_wired_in_main_window():
    """记录现状：``connect_autosave()`` 只在主窗口构造时调用一次。

    这不是缺陷，但是一条**必须被看见的前提**：右键快速调用不构造 MainWindow，
    因此整条快速调用链上没有任何配置持久化。今天安全，是因为快速调用链只读配置
    （见下一个用例）。
    """
    hits = []
    for mod_name in ("momentshift.gui.main_window", "momentshift.quick_runner"):
        mod = importlib.import_module(mod_name)
        src = inspect.getsource(mod)
        if "connect_autosave()" in src.replace("``connect_autosave()``", ""):
            hits.append(mod_name)
    assert "momentshift.gui.main_window" in hits
    assert "momentshift.quick_runner" not in hits, (
        "quick_runner 出现了 connect_autosave() —— 现状记录用例需要更新"
    )


@pytest.mark.parametrize(
    "mod_name",
    [
        "momentshift.gui.convert_setup_dialog",
        "momentshift.gui.quick_dialogs",
    ],
)
def test_quick_path_modules_do_not_write_config(mod_name):
    """快速调用链上的模块**只读配置、不写配置**。

    ODD-22 之后配置落盘完全依赖 ``connect_autosave()``，而它只在主窗口里接。
    一旦有人在这两个模块里写 ``cfg.xxx.value = ...``，改动在右键快速调用模式下
    **不会落盘**，且没有任何报错 —— 这条静态守卫就是为了让那次改动当场变红。
    """
    mod = importlib.import_module(mod_name)
    tree = ast.parse(inspect.getsource(mod))
    writes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            # 匹配形如 cfg.<item>.value = ...
            if (
                isinstance(tgt, ast.Attribute)
                and tgt.attr == "value"
                and isinstance(tgt.value, ast.Attribute)
                and isinstance(tgt.value.value, ast.Name)
                and tgt.value.value.id == "cfg"
            ):
                writes.append(f"cfg.{tgt.value.attr}.value @L{node.lineno}")
    assert not writes, (
        f"{mod_name} 写了配置但快速调用模式下不会落盘：{writes}。"
        "要么改用主窗口路径，要么在 quick_runner._setup_app 之后补 connect_autosave()。"
    )
