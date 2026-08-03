"""``gui.animations`` 的纯 Python 单测 + 两条铁律的静态守卫。

为什么这里**不建 QApplication**：``tests/unit`` 全体用例目前都是无 Qt 环境的，
一旦在其中拉起 Qt 平台插件，pytest worker 在沙箱里会被硬杀（这也是
``pyproject.toml`` 把 ``testpaths`` 钉在 ``tests/unit`` 的原因）。
因此本文件只覆盖「不需要活控件」的部分：

- 令牌取值与相互关系；
- ``blend_color`` 的数值/格式契约（``QColor`` 不需要 QApplication）；
- 全局开关的语义与环境变量解析；
- 两条铁律的**源码静态守卫**（扫 ``gui/*.py`` 的语法树与文本）。

需要活控件的部分（parent 归属、逐帧插值、稳态字符串）放在
``tests/offscreen_smoke.py`` 的 v0.8.0 B2/B3 段落里。
"""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest
from PyQt6.QtCore import QEasingCurve

from momentshift.gui import animations

_GUI_DIR = Path(animations.__file__).parent


@pytest.fixture(autouse=True)
def _restore_global_switch():
    """每个用例后把全局开关复位，避免用例之间互相污染。"""
    before = animations.animations_enabled()
    yield
    animations.set_animations_enabled(before)


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------
def test_duration_tokens_are_strictly_ordered():
    """三档时长必须严格递增，否则「档位」这个概念就没有意义了。"""
    assert (
        animations.DURATION_FAST
        < animations.DURATION_MEDIUM
        < animations.DURATION_CARD
        < animations.DURATION_SLOW
    )


def test_fast_duration_stays_imperceptible():
    """跟随型动效超过 ~150ms 会被感知成「界面反应慢」。"""
    assert animations.DURATION_FAST <= 150


def test_card_duration_keeps_the_legacy_value():
    """B3 接入点 6 只是把 250ms 搬了个家，**不许**趁机改手感。"""
    assert animations.DURATION_CARD == 250


def test_entrance_curve_matches_legacy_collapsible_card():
    """折叠卡片原本就是 OutCubic，迁移后必须还是它。"""
    assert animations.CURVE_IN is QEasingCurve.Type.OutCubic


@pytest.mark.parametrize("name", ["CURVE_IN", "CURVE_OUT", "CURVE_SMOOTH"])
def test_curve_tokens_are_easing_curve_types(name):
    assert isinstance(getattr(animations, name), QEasingCurve.Type)


def test_every_curve_token_has_a_real_consumer():
    """令牌表里不许有「预留」条目。

    没有消费方的令牌不会保持正确：它不参与任何回归，改坏了也没人发现，
    还会让后来者误以为某处正在用它。需要新曲线时跟着第一个接入点一起加。
    """
    exported = {n for n in dir(animations) if n.startswith("CURVE_")}
    consumers = "\n".join(
        p.read_text(encoding="utf-8") for p in _gui_sources() if p.name != "animations.py"
    )
    own = Path(animations.__file__).read_text(encoding="utf-8")
    orphans = sorted(
        name
        for name in exported
        if f"animations.{name}" not in consumers
        and own.count(name) < 2  # 除定义处外，模块内部至少还得用到一次
    )
    assert orphans == [], f"这些缓动令牌没有任何消费方：{orphans}"


def test_slide_offset_fits_inside_the_row_bottom_margin():
    """进场位移量必须小于行的下内边距（12），否则「借」不出空间会改变总高。"""
    assert 0 < animations.SLIDE_OFFSET < 12


def test_batch_thresholds_are_sane():
    """阈值要覆盖「肉眼能同时看到的行数」，窗口要短到能区分两次独立操作。"""
    assert 8 <= animations.QUEUE_ANIM_BATCH_LIMIT <= 64
    assert 0 < animations.QUEUE_BURST_WINDOW_MS <= 200


def test_progress_min_step_is_a_percentage_point():
    assert 1 <= animations.PROGRESS_SMOOTH_MIN_STEP <= 10


# ---------------------------------------------------------------------------
# blend_color
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("t,expected", [(0.0, "#000000"), (1.0, "#ffffff"), (0.5, "#808080")])
def test_blend_color_interpolates_linearly(t, expected):
    assert animations.blend_color("#000000", "#FFFFFF", t) == expected


@pytest.mark.parametrize("t", [-1.0, -0.001, 1.001, 99.0])
def test_blend_color_clamps_out_of_range_factors(t):
    """越界因子夹回 [0,1]，绝不返回非法色值。"""
    out = animations.blend_color("#000000", "#FFFFFF", t)
    assert out in ("#000000", "#ffffff")


def test_blend_color_always_returns_lowercase_hex():
    """小写是 ``QColor`` 的事实；这正是稳态**禁止**用它的原因。"""
    out = animations.blend_color("#3EB68F", "#FF7279", 0.37)
    assert re.fullmatch(r"#[0-9a-f]{6}", out), out


def test_blend_color_endpoints_preserve_the_token_value():
    """端点取值必须等于令牌本身（只是大小写不同）。"""
    assert animations.blend_color("#3EB68F", "#FF7279", 0.0) == "#3eb68f"
    assert animations.blend_color("#3EB68F", "#FF7279", 1.0) == "#ff7279"


# ---------------------------------------------------------------------------
# 全局开关
# ---------------------------------------------------------------------------
def test_global_switch_round_trips():
    animations.set_animations_enabled(False)
    assert animations.animations_enabled() is False
    animations.set_animations_enabled(True)
    assert animations.animations_enabled() is True


def test_should_animate_without_widget_follows_the_global_switch():
    animations.set_animations_enabled(True)
    assert animations.should_animate() is True
    animations.set_animations_enabled(False)
    assert animations.should_animate() is False


class _FakeWidget:
    """只实现 ``isVisible()`` 的替身，用来免 Qt 地测可见性分支。"""

    def __init__(self, visible: bool | None):
        self._visible = visible

    def isVisible(self) -> bool:  # noqa: N802 - 对齐 Qt 命名
        if self._visible is None:
            raise RuntimeError("wrapped C/C++ object has been deleted")
        return self._visible


def test_should_animate_requires_the_widget_to_be_visible():
    """看不见的动效只有开销没有价值 —— 所有离屏门禁靠这条自动走无动画路径。"""
    animations.set_animations_enabled(True)
    assert animations.should_animate(_FakeWidget(True)) is True
    assert animations.should_animate(_FakeWidget(False)) is False


def test_should_animate_survives_a_destroyed_widget():
    """C++ 侧已析构、Python 壳还在时不许抛，直接判定为不播。"""
    animations.set_animations_enabled(True)
    assert animations.should_animate(_FakeWidget(None)) is False


def test_should_animate_short_circuits_when_globally_off():
    animations.set_animations_enabled(False)
    assert animations.should_animate(_FakeWidget(True)) is False


@pytest.mark.parametrize("raw", ["0", "false", "off", "no", "OFF", " Off "])
def test_env_var_disables_animations(monkeypatch, raw):
    monkeypatch.setenv("MOMENTSHIFT_ANIMATIONS", raw)
    reloaded = importlib.reload(animations)
    try:
        assert reloaded.ANIMATIONS_ENABLED is False
    finally:
        monkeypatch.delenv("MOMENTSHIFT_ANIMATIONS", raising=False)
        importlib.reload(animations)


@pytest.mark.parametrize("raw", ["1", "true", "on", "yes", "whatever"])
def test_env_var_keeps_animations_on_for_anything_else(monkeypatch, raw):
    monkeypatch.setenv("MOMENTSHIFT_ANIMATIONS", raw)
    reloaded = importlib.reload(animations)
    try:
        assert reloaded.ANIMATIONS_ENABLED is True
    finally:
        monkeypatch.delenv("MOMENTSHIFT_ANIMATIONS", raising=False)
        importlib.reload(animations)


def test_animations_are_on_by_default(monkeypatch):
    monkeypatch.delenv("MOMENTSHIFT_ANIMATIONS", raising=False)
    reloaded = importlib.reload(animations)
    assert reloaded.ANIMATIONS_ENABLED is True


# ---------------------------------------------------------------------------
# 铁律一的静态守卫：QPropertyAnimation 必须带 parent
# ---------------------------------------------------------------------------
def _gui_sources() -> list[Path]:
    return sorted(p for p in _GUI_DIR.glob("*.py") if p.name != "__init__.py")


def _property_animation_calls(tree: ast.AST) -> list[ast.Call]:
    """取出语法树里所有真实的 ``QPropertyAnimation(...)`` 调用节点。

    这里刻意用 ``ast`` 而不是正则扫文本，有三个理由：

    1. ``animations.py`` 的文档串里写着反例示范（「为什么不用
       ``QPropertyAnimation(widget, b"pos")``」），正则会把这段**解释踩坑的文字**
       当成违规代码误报——守卫反过来惩罚写注释的人，是最坏的守卫；
    2. 跨行书写的调用正则接不住，容易漏报；
    3. 关键字实参 ``parent=`` 正则也数不准。

    ``ast`` 三者全免疫：它只看真实调用节点。
    """
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "QPropertyAnimation":
            calls.append(node)
    return calls


def _unparented_property_animations(source: str, label: str = "<src>") -> list[str]:
    """返回 ``source`` 里所有没给 parent 的 ``QPropertyAnimation`` 调用位置。"""
    offenders: list[str] = []
    for call in _property_animation_calls(ast.parse(source, filename=label)):
        has_parent_kw = any(kw.arg == "parent" for kw in call.keywords)
        if len(call.args) < 3 and not has_parent_kw:
            offenders.append(f"{label}:{call.lineno}")
    return offenders


def test_every_property_animation_is_parented():
    """v0.7.19 / v0.7.24 两次崩溃的根因，用静态扫描永久钉死。

    ``QPropertyAnimation(target, prop)`` 只有两个实参时对象没有 parent，
    函数一返回就可能被 Python GC 回收，表现为「动画偶尔不播」这种最难复现的
    偶发 bug。全项目只允许「第三个位置实参」或「显式 ``parent=``」两种写法。
    """
    offenders: list[str] = []
    for path in _gui_sources():
        offenders += _unparented_property_animations(path.read_text(encoding="utf-8"), path.name)
    assert offenders == [], "QPropertyAnimation 缺少 parent：\n" + "\n".join(offenders)


def test_the_parent_guard_itself_can_still_fail():
    """守卫也要被守卫：证明它不是一条「永远通过」的空断言。

    静态检查最常见的腐坏方式不是误报而是**静默失效**——某次重构后正则/遍历
    再也匹配不到任何东西，测试依旧全绿，护栏却已经形同虚设。所以这里显式喂
    三段样本，验证「违规能被抓到、合规不被误伤、文档串不被误伤」。
    """
    assert _unparented_property_animations('QPropertyAnimation(self, b"x")')
    assert not _unparented_property_animations('QPropertyAnimation(self, b"x", self)')
    assert not _unparented_property_animations('QPropertyAnimation(self, b"x", parent=self)')
    assert not _unparented_property_animations('"""不要写 QPropertyAnimation(w, b"x")"""')


# ---------------------------------------------------------------------------
# 铁律二的静态守卫：setMask 与透明度特效互斥
# ---------------------------------------------------------------------------
def test_widgets_using_set_mask_never_take_the_opacity_path():
    """有 setMask 的模块不许出现 ``QGraphicsOpacityEffect`` / ``animations.fade``。

    ``QGraphicsOpacityEffect`` 会把控件转成离屏合成，与 ``setMask(QRegion(...))``
    的裁剪路径叠加后在部分平台出现黑边/缺角。此处只允许走 ``blend_color``。
    """
    for path in _gui_sources():
        text = path.read_text(encoding="utf-8")
        if "setMask(" not in text or path.name == "animations.py":
            continue
        # 注释/文档串里提到名字是允许的，这里只拦真正的调用
        assert "QGraphicsOpacityEffect(" not in text, path.name
        assert "animations.fade(" not in text, path.name


# ---------------------------------------------------------------------------
# B3 接入完成度的静态守卫
#
# 这条守卫**只有在 B3 落地后才成立**（B4 时 ``theme.py`` 仍硬编码
# ``QEasingCurve.Type.OutCubic``），所以它随 B3 提交，不随 B2 提交——
# 让每个 commit 单独 checkout 出来都是绿的，bisect 才有意义。
# ---------------------------------------------------------------------------
def test_animations_module_is_the_only_place_holding_easing_literals():
    """缓动曲线是「怎么动」的一部分，必须收在单点真源里。"""
    offenders = [
        p.name
        for p in _gui_sources()
        if p.name != "animations.py" and "QEasingCurve.Type." in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"这些模块仍在硬编码缓动曲线：{offenders}"


def test_animations_module_does_not_import_sibling_gui_modules():
    """单点真源必须是叶子：它一旦回头 import gui 里别的模块就会成环。"""
    text = Path(animations.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^from \.\S* import", text, re.MULTILINE)
    assert not re.search(r"^from \. import", text, re.MULTILINE)
