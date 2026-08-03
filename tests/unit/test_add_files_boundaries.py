"""v0.8.0 门禁盲区补测：``ConversionManager.add_files`` 与 ``_expand_paths`` 的
边界 / 异常输入。

这些路径是现有门禁（ruff / pytest / offscreen_smoke / qss_snapshot / config_coverage /
i18n_coverage）共同漏掉的：跨模块状态流、异常路径、边界输入（空文件列表、同名输出
冲突、磁盘写入权限/占位冲突）。全部离屏、不需要真实 ffmpeg、不需要网络。

覆盖：
- ``_expand_paths``：空列表 / 全丢失 / 目录遍历 / 扩展名大小写 / 精确去重；
  以及两个**已知缺陷**（预存在，非 B4 引入）：分隔符变体重复、目录与其内部文件同拖。
- ``ConversionManager.add_files``：空列表 / 全丢失 / 无法识别分类 / 输出目录被已存在
  文件占用（异常路径）；以及两个**已知输出路径冲突缺陷**（预存在，非 B4 引入）：
  固定模式下不同源目录同名、同源不同扩展同名。

业务代码（src/）一律不改动，缺陷用 ``pytest.mark.xfail(strict=True)`` 显式登记：
行为当前不满足预期时不让门禁绿，未来修复后 xfail 会变 XPASS 提醒回收。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from momentshift.core import queue as queue_mod
from momentshift.core.queue import ConversionManager
from momentshift.gui.base import InterfaceBase


# ---------------------------------------------------------------------------
# _expand_paths
# ---------------------------------------------------------------------------
def _expand(self, paths, exts):
    return InterfaceBase._expand_paths(self, paths, exts)


class _Bare:
    pass


IMG_EXTS = {".png", ".jpg", ".jpeg"}


def test_expand_empty_returns_empty():
    assert _expand(_Bare(), [], IMG_EXTS) == []


def test_expand_missing_files_skipped():
    d = tempfile.mkdtemp()
    assert _expand(_Bare(), [os.path.join(d, "ghost.png")], IMG_EXTS) == []


def test_expand_walks_directory_and_filters_ext():
    d = tempfile.mkdtemp()
    sub = os.path.join(d, "sub")
    os.makedirs(sub)
    open(os.path.join(d, "a.png"), "w").write("x")
    open(os.path.join(d, "skip.txt"), "w").write("x")
    open(os.path.join(sub, "b.PNG"), "w").write("x")  # 大写扩展名
    out = _expand(_Bare(), [d], IMG_EXTS)
    names = sorted(os.path.basename(p) for p in out)
    assert names == ["a.png", "b.PNG"]
    assert len(out) == 2


def test_expand_exact_duplicate_deduped():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "a.png")
    open(p, "w").write("x")
    out = _expand(_Bare(), [p, p, p], IMG_EXTS)
    assert len(out) == 1


@pytest.mark.xfail(
    reason="已知缺陷（预存在）：去重按精确路径字符串，Qt 拖拽给的是正斜杠、"
    "os.walk 给反斜杠。同一文件的正反斜杠两种写法会被当成两条，导致重复入队。",
    strict=True,
)
def test_expand_separator_variant_not_deduped():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "a.png")
    open(p, "w").write("x")
    fwd = d.replace("\\", "/") + "/a.png"  # Qt 风格正斜杠
    out = _expand(_Bare(), [p, fwd], IMG_EXTS)
    assert len(out) == 1  # 期望去重；当前会返回 2


@pytest.mark.xfail(
    reason="已知缺陷（预存在）：一次拖拽同时拖入某文件夹与其内部文件时，"
    "文件夹遍历（os.walk 反斜杠）与文件列表（Qt 正斜杠）给出同一文件的两套路径，"
    "精确去重无法合并，导致该文件被重复入队。",
    strict=True,
)
def test_expand_folder_and_inner_file_duplicate():
    d = tempfile.mkdtemp()
    sub = os.path.join(d, "sub")
    os.makedirs(sub)
    inner = os.path.join(sub, "a.png")
    open(inner, "w").write("x")
    fwd_inner = inner.replace("\\", "/")  # Qt 风格
    out = _expand(_Bare(), [d, fwd_inner], IMG_EXTS)
    assert len([x for x in out if x.endswith("a.png")]) == 1  # 期望 1；当前 2


# ---------------------------------------------------------------------------
# ConversionManager.add_files
# ---------------------------------------------------------------------------
@pytest.fixture
def mgr():
    # 构造不探测 ffmpeg，避免起子进程
    queue_mod.find_ffmpeg = lambda *a, **k: None
    return ConversionManager()


def _mk(path, ext="jpg", content=b"\xff\xd8\xff"):
    p = path if path.endswith(ext) else path + "." + ext
    with open(p, "wb") as f:
        f.write(content)
    return p


def test_add_files_empty_list_returns_nothing(mgr):
    d = tempfile.mkdtemp()
    added, skipped = mgr.add_files([], "png", d, False)
    assert added == [] and skipped == []


def test_add_files_all_missing_returns_nothing(mgr):
    d = tempfile.mkdtemp()
    added, skipped = mgr.add_files([os.path.join(d, "nope.jpg")], "png", d, False)
    assert added == [] and skipped == []


def test_add_files_unrecognized_category_skipped(mgr):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "doc.txt")
    open(p, "w").write("hello")
    added, skipped = mgr.add_files([p], "png", d, False)
    assert added == []
    assert "doc.txt" in skipped


@pytest.mark.xfail(
    reason="已知缺陷（预存在，非 B4 引入）：固定输出模式下，不同源目录的同名文件"
    "会映射到同一个 output_path（_unique_path 只看目标是否已存在于磁盘，不看队列内"
    "是否已有相同目标）。两任务争抢同一输出，后写覆盖先写。",
    strict=True,
)
def test_add_files_collision_same_stem_diff_dir(mgr):
    root = tempfile.mkdtemp()
    out = os.path.join(root, "out")
    os.makedirs(out)
    A = os.path.join(root, "A")
    B = os.path.join(root, "B")
    os.makedirs(A)
    os.makedirs(B)
    pa = _mk(os.path.join(A, "photo"))
    pb = _mk(os.path.join(B, "photo"))
    added, _ = mgr.add_files([pa, pb], "png", out, False, output_mode="fixed")
    outs = [t.output_path for t in added]
    assert len(set(outs)) == len(outs)  # 期望各唯一；当前相同


@pytest.mark.xfail(
    reason="已知缺陷（预存在，非 B4 引入）：同源不同扩展、目标格式相同（same 模式）"
    "时，主名相同 → 输出路径相同，两任务争抢同一输出文件。",
    strict=True,
)
def test_add_files_collision_same_dir_diff_ext_same_target(mgr):
    root = tempfile.mkdtemp()
    p1 = _mk(os.path.join(root, "pic"), "jpg")
    p2 = _mk(os.path.join(root, "pic"), "png")
    added, _ = mgr.add_files([p1, p2], "webp", None, False, output_mode="same", suffix="")
    outs = [t.output_path for t in added]
    assert len(set(outs)) == len(outs)  # 期望各唯一；当前相同


def test_add_files_unique_path_renames_when_disk_collision(mgr):
    """磁盘上已存在同名输出时，_unique_path 应自动加 _1 规避。"""
    root = tempfile.mkdtemp()
    out = os.path.join(root, "out")
    os.makedirs(out)
    existing = os.path.join(out, "photo.png")
    open(existing, "wb").write(b"old")  # 预置一个已存在文件
    A = os.path.join(root, "A")
    os.makedirs(A)
    pa = _mk(os.path.join(A, "photo"))
    added, _ = mgr.add_files([pa], "png", out, False, output_mode="fixed")
    assert added[0].output_path.endswith("photo_1.png")


@pytest.mark.xfail(
    reason="已知缺陷（预存在）：output_dir 指向一个已存在的「文件」而非目录时，"
    "add_files 内部 queue.py:355 的 out_dir.mkdir(parents=True) 抛 FileExistsError 且"
    "无人捕获，一路冒泡到 convert_setup_dialog._on_confirm。主窗口路径被 "
    "__main__.Application.notify 兜住 → 表现为「点确定毫无反应、弹窗不关」；"
    "快速调用路径没装该兜底 → 直接闪退。",
    strict=True,
)
def test_add_files_output_dir_is_a_file_raises_handled(mgr):
    root = tempfile.mkdtemp()
    src = _mk(os.path.join(root, "a"))
    bad = os.path.join(root, "iamafile")
    open(bad, "w").write("x")  # 已存在的文件，而非目录
    # 期望：优雅处理（返回空 / 跳过），而不是把 FileExistsError 抛给 UI 线程
    mgr.add_files([src], "png", bad, False, output_mode="fixed")


@pytest.mark.xfail(
    reason="已知缺陷（预存在，可复现度高）：固定输出目录指向已断开的盘符"
    "（U 盘拔了 / 网络盘掉线 / 外置硬盘没挂上）时，mkdir 抛 FileNotFoundError，"
    "同样无人捕获。这是比「目录被文件占用」现实得多的触发路径 —— 用户把输出目录"
    "设成移动硬盘后拔掉即可复现。",
    strict=True,
)
def test_add_files_output_dir_on_missing_drive_handled(mgr):
    root = tempfile.mkdtemp()
    src = _mk(os.path.join(root, "a"))
    # 选一个几乎不可能存在的盘符
    gone = "Z:" + os.sep + "MomentShiftOut"
    if os.path.exists("Z:" + os.sep):  # 万一真有 Z 盘就跳过
        pytest.skip("本机存在 Z 盘，换不到断开盘符")
    mgr.add_files([src], "png", gone, False, output_mode="fixed")


def test_add_files_reserved_name_and_long_path_do_not_raise(mgr):
    """反向确认：Windows 保留名 con / 超长目录名不会抛（避免过度归因）。"""
    root = tempfile.mkdtemp()
    src = _mk(os.path.join(root, "a"))
    for bad in (os.path.join(root, "con"), os.path.join(root, "d" * 200)):
        added, _ = mgr.add_files([src], "png", bad, False, output_mode="fixed")
        assert added, f"{bad} 不该被静默丢弃"
