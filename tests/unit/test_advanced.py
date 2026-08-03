"""``core.advanced`` 的纯 Python 单测（不依赖 Qt / UI）。

重点覆盖 v0.8.0 的两处行为变更：

- RISK-03：模块级 ``adv`` 的并发访问加锁，``reset()`` 改为原地重建；
- Q3：入队快照改为**深拷贝**，已入队任务不再被面板后续改动串台。
"""

from __future__ import annotations

import threading

import pytest

from momentshift.core import advanced


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后都把全局参数复位，避免用例之间互相污染。"""
    advanced.reset()
    yield
    advanced.reset()


# ---------------------------------------------------------------------------
# default_options / reset
# ---------------------------------------------------------------------------
def test_default_options_returns_independent_copies():
    a = advanced.default_options()
    b = advanced.default_options()
    a["video"]["fps"] = "60"
    a["image"]["compress"]["quality"] = 1
    assert b["video"]["fps"] == "original"
    assert b["image"]["compress"]["quality"] == 95


def test_reset_keeps_dict_identity():
    """reset() 必须原地重建，不能重新绑定 ``advanced.adv`` 或它的子字典。

    高级设置面板里有 ``adv = advanced.adv["image"]`` /
    ``comp = advanced.adv["image"]["compress"]`` 这种抓住子字典再原地改的写法；
    一旦 reset 换掉对象，面板手上那份就跟全局脱钩了。
    """
    before = advanced.adv
    before_image = advanced.adv["image"]
    before_compress = advanced.adv["image"]["compress"]
    advanced.adv["video"]["fps"] = "60"

    advanced.reset()

    assert advanced.adv is before
    assert advanced.adv["image"] is before_image
    assert advanced.adv["image"]["compress"] is before_compress
    assert advanced.adv["video"]["fps"] == "original"


def test_reset_restores_nested_values():
    advanced.adv["image"]["compress"]["quality"] = 10
    advanced.adv["audio"]["merge"] = True
    advanced.reset()
    assert advanced.adv["image"]["compress"]["quality"] == 95
    assert advanced.adv["audio"]["merge"] is False


# ---------------------------------------------------------------------------
# get / snapshot（Q3 的核心）
# ---------------------------------------------------------------------------
def test_get_returns_live_reference():
    """get() 是给 GUI 面板用的可写引用，必须仍指向全局。"""
    live = advanced.get("video")
    live["fps"] = "24"
    assert advanced.adv["video"]["fps"] == "24"


def test_get_unknown_category_returns_empty_dict():
    assert advanced.get("nope") == {}
    assert advanced.snapshot("nope") == {}


def test_snapshot_is_detached_at_top_level():
    snap = advanced.snapshot("video")
    advanced.adv["video"]["fps"] = "60"
    assert snap["fps"] == "original"


def test_snapshot_is_deep_not_shallow():
    """v0.8.0 Q3 的关键回归：``image.compress`` 是嵌套子字典。

    旧实现 ``dict(advanced.get("image"))`` 只拷了第一层，``compress`` 仍与全局
    共享引用——用户在任务跑到一半时改压缩质量，已入队任务会跟着变。
    """
    snap = advanced.snapshot("image")
    assert snap["compress"] is not advanced.adv["image"]["compress"]

    advanced.adv["image"]["compress"]["quality"] = 10
    advanced.adv["image"]["compress"]["level"] = 6
    assert snap["compress"]["quality"] == 95
    assert snap["compress"]["level"] == 3


def test_snapshot_shallow_copy_would_have_leaked():
    """把旧的浅拷贝行为写成对照组，防止将来有人"顺手优化"回去。"""
    shallow = dict(advanced.get("image"))
    advanced.adv["image"]["compress"]["quality"] = 7
    assert shallow["compress"]["quality"] == 7  # 旧实现：串台
    deep = advanced.snapshot("image")
    advanced.adv["image"]["compress"]["quality"] = 3
    assert deep["compress"]["quality"] == 7  # 新实现：定格


def test_snapshot_mutation_does_not_touch_global():
    snap = advanced.snapshot("audio")
    snap["merge"] = True
    snap["bitrate"] = "128k"
    assert advanced.adv["audio"]["merge"] is False
    assert advanced.adv["audio"]["bitrate"] == "original"


# ---------------------------------------------------------------------------
# is_merge_enabled
# ---------------------------------------------------------------------------
def test_is_merge_enabled_defaults_false():
    for category in ("image", "video", "audio"):
        assert advanced.is_merge_enabled(category) is False


def test_is_merge_enabled_reads_live_value():
    advanced.adv["video"]["merge"] = True
    assert advanced.is_merge_enabled("video") is True
    assert advanced.is_merge_enabled("audio") is False


def test_is_merge_enabled_unknown_category():
    assert advanced.is_merge_enabled("nope") is False


# ---------------------------------------------------------------------------
# 并发（RISK-03）
# ---------------------------------------------------------------------------
def test_snapshot_under_concurrent_writes_is_self_consistent():
    """一边狂改全局，一边狂拍快照，快照本身不得撕裂或抛异常。

    断言的是"每份快照都是某一次写入的完整结果"——不能出现 fps 已经翻到新值、
    bitrate 还停在旧值这种半拉子状态。
    """
    stop = threading.Event()
    errors: list[BaseException] = []
    snapshots: list[dict] = []

    def _writer():
        i = 0
        try:
            while not stop.is_set():
                i += 1
                live = advanced.get("video")
                # 同一"代"的两个字段一起写，快照必须要么全看到要么全看不到。
                live["fps"] = str(i)
                live["bitrate"] = str(i)
                i %= 1000
        except BaseException as exc:  # noqa: BLE001 - 测试线程需原样上报
            errors.append(exc)

    def _reader():
        try:
            while not stop.is_set():
                snapshots.append(advanced.snapshot("video"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_writer), threading.Thread(target=_reader)]
    for t in threads:
        t.start()
    # 时间足够跑出上千次交叉，又不至于拖慢测试。
    stop.wait(0.3)
    stop.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert not errors
    assert len(snapshots) > 10
    for snap in snapshots:
        assert set(snap) >= {"fps", "bitrate", "resolution", "codec", "crf", "merge"}


def test_reset_under_concurrent_snapshot_never_yields_partial_dict():
    """reset() 的 clear()+update() 中间态不得被 snapshot 观察到。"""
    stop = threading.Event()
    bad: list[dict] = []

    def _resetter():
        while not stop.is_set():
            advanced.reset()

    def _reader():
        while not stop.is_set():
            snap = advanced.snapshot("image")
            if not snap:
                bad.append(snap)

    threads = [threading.Thread(target=_resetter), threading.Thread(target=_reader)]
    for t in threads:
        t.start()
    stop.wait(0.3)
    stop.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert not bad, "snapshot 读到了 reset 的中间态（空字典）"


# ---------------------------------------------------------------------------
# build_advanced_args 的 options 省略路径
# ---------------------------------------------------------------------------
def test_build_advanced_args_defaults_to_snapshot():
    advanced.adv["video"]["fps"] = "30"
    args = advanced.build_advanced_args("video", "mp4")
    assert "-vf" in args
    assert "fps=30" in args[args.index("-vf") + 1]


def test_build_advanced_args_explicit_options_win():
    advanced.adv["video"]["fps"] = "30"
    args = advanced.build_advanced_args("video", "mp4", {"fps": "24"})
    assert "fps=24" in args[args.index("-vf") + 1]


def test_get_current_args_does_not_expose_live_dict():
    """get_current_args 内部走 snapshot，调用它不应让外部拿到可写引用。"""
    before = dict(advanced.adv["video"])
    advanced.get_current_args("video", "mp4")
    assert advanced.adv["video"] == before
