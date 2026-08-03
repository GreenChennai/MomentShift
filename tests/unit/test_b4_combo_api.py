"""B4 / ODD-07：映射型下拉框公开 API 的**行为等价性**回归。

背景
----
v0.8.0-B4 把三处「往 ComboBox 上挂 ``_mapping`` 私有属性、再在别的模块里直接读」
的写法，收口成 ``gui/base`` 的四个公开函数：``bind_combo_mapping`` /
``combo_mapping`` / ``combo_value`` / ``select_combo_value``。

收口顺带**换掉了选中算法**：

- 改造前按「映射列表的下标」定位：``for i, (disp, val) in enumerate(mapping)``，
  命中即 ``setCurrentIndex(i)``。
- 改造后按「显示文案」定位：遍历 ``dict(mapping)`` 找到值相等的那一项，
  再 ``combo.findText(disp)``。

这两种算法只有在「同一份 mapping 里存在**重复的显示文案**」时才会分叉
（``dict()`` 会把重复文案折叠掉一项，下标与行号从此对不上）。本文件做两件事：

1. 用参考实现复刻改造前算法，在一批语料上断言新旧结果逐条相同；
2. 静态扫描源码里所有字面量 mapping，断言三语下都不存在重复显示文案 ——
   也就是证明「那个唯一的分叉点在本项目里不可达」。

为什么不建 QApplication：这四个函数全是鸭子类型的，只用到
``getattr/setattr/currentText/findText/setCurrentIndex``，用假控件就能测全，
真控件反而会把 pytest worker 拖进 Qt 平台插件（见 pyproject 的 testpaths 注释）。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from momentshift.gui import base as gui_base

_SRC = Path(gui_base.__file__).resolve().parents[1]
_GUI_DIR = _SRC / "gui"
_LOCALES_DIR = _SRC / "i18n" / "locales"
_LOCALES = ("zh_CN", "zh_TW", "en_US")


# ---------------------------------------------------------------------------
# 假控件：只实现四个函数真正用到的那几个方法
# ---------------------------------------------------------------------------
class FakeCombo:
    """鸭子类型的 ComboBox 替身，语义对齐 qfluentwidgets.ComboBoxBase。

    对齐点（照抄库实现，否则测的就不是真行为）：
    - ``addItem`` 加入第一项时自动选中它；
    - ``setCurrentIndex`` 越界或与当前值相同时静默 no-op；
    - ``currentText`` 在无选中项时返回空串；
    - ``findText`` 返回**第一个**同文案项的下标，找不到返回 -1。
    """

    def __init__(self, texts=()):
        self.items: list[str] = []
        self._index = -1
        for t in texts:
            self.addItem(t)

    def addItem(self, text: str) -> None:
        self.items.append(text)
        if len(self.items) == 1:
            self.setCurrentIndex(0)

    def clear(self) -> None:
        self.items.clear()
        self._index = -1

    def currentIndex(self) -> int:
        return self._index

    def setCurrentIndex(self, index: int) -> None:
        if not 0 <= index < len(self.items) or index == self._index:
            return
        self._index = index

    def currentText(self) -> str:
        if not 0 <= self._index < len(self.items):
            return ""
        return self.items[self._index]

    def findText(self, text: str) -> int:
        for i, t in enumerate(self.items):
            if t == text:
                return i
        return -1


def _combo_from(mapping) -> FakeCombo:
    return FakeCombo([disp for disp, _ in mapping])


# ---------------------------------------------------------------------------
# 四个公开函数的契约
# ---------------------------------------------------------------------------
def test_bind_then_read_roundtrip():
    """绑上去的映射必须原样读得回来。"""
    mapping = [("自动选择", "auto"), ("OxiPNG", "oxipng")]
    combo = _combo_from(mapping)
    gui_base.bind_combo_mapping(combo, mapping)
    assert gui_base.combo_mapping(combo) == dict(mapping)


def test_combo_mapping_returns_a_copy():
    """必须返回拷贝：quick_dialogs 会就地裁剪候选项，不能改到控件真身。

    这是 B4 刻意写进 docstring 的契约。丢掉拷贝的后果是「右键快速压缩弹窗
    里删掉了『自动选择』，主窗口的下拉框跟着也没了」。
    """
    mapping = [("自动选择", "auto"), ("OxiPNG", "oxipng")]
    combo = _combo_from(mapping)
    gui_base.bind_combo_mapping(combo, mapping)

    borrowed = gui_base.combo_mapping(combo)
    borrowed.pop("自动选择")
    borrowed["注入"] = "x"

    assert gui_base.combo_mapping(combo) == dict(mapping)


def test_bind_snapshots_the_source_mapping():
    """绑定时应固化一份 dict，之后改原列表/原 dict 不应影响控件。"""
    src = {"A": "a", "B": "b"}
    combo = FakeCombo(["A", "B"])
    gui_base.bind_combo_mapping(combo, src)
    src["A"] = "changed"
    assert gui_base.combo_mapping(combo)["A"] == "a"


def test_combo_mapping_on_unbound_combo_is_empty():
    assert gui_base.combo_mapping(FakeCombo(["A"])) == {}


def test_combo_mapping_tolerates_none_attribute():
    """``_mapping`` 被置成 None 时按空映射处理（``or {}`` 分支）。"""
    combo = FakeCombo(["A"])
    combo._mapping = None
    assert gui_base.combo_mapping(combo) == {}


def test_combo_value_falls_back_to_display_text():
    """未绑映射 / 文案不在映射里 → 回退为显示文案本身。

    这是改造前 ``combo._mapping.get(t, t)`` 的兜底，必须一比一保留：
    advanced_panel 的 ``_sync(combo_value(backend))`` 依赖它，
    回退成 None 或空串会让「压缩后端」分组显示错乱。
    """
    unbound = FakeCombo(["PNG"])
    assert gui_base.combo_value(unbound) == "PNG"

    partial = FakeCombo(["PNG", "JPG"])
    gui_base.bind_combo_mapping(partial, [("JPG", "jpg")])
    assert gui_base.combo_value(partial) == "PNG"


def test_combo_value_on_empty_combo_returns_empty_string():
    """空下拉框（clear 之后）取值不许抛异常。"""
    combo = FakeCombo()
    gui_base.bind_combo_mapping(combo, [("A", "a")])
    assert gui_base.combo_value(combo) == ""


def test_select_combo_value_hit_and_miss():
    mapping = [("PNG", "png"), ("JPG", "jpg"), ("WebP", "webp")]
    combo = _combo_from(mapping)
    gui_base.bind_combo_mapping(combo, mapping)

    assert gui_base.select_combo_value(combo, "webp") is True
    assert combo.currentText() == "WebP"

    # 未命中：返回 False 且**不改动**当前选择
    assert gui_base.select_combo_value(combo, "tiff") is False
    assert combo.currentText() == "WebP"


def test_select_combo_value_on_unbound_combo_is_a_noop():
    combo = FakeCombo(["PNG", "JPG"])
    assert gui_base.select_combo_value(combo, "jpg") is False
    assert combo.currentText() == "PNG"


def test_select_combo_value_ignores_stale_mapping_entries():
    """映射里有、但控件里已经 removeItem 掉的项，不许被选中。

    quick_dialogs 裁剪候选项走的就是「控件删项、映射不动」这条路。
    """
    combo = FakeCombo(["OxiPNG"])  # 「自动选择」已被删掉
    gui_base.bind_combo_mapping(combo, [("自动选择", "auto"), ("OxiPNG", "oxipng")])
    assert gui_base.select_combo_value(combo, "auto") is False
    assert combo.currentText() == "OxiPNG"


# ---------------------------------------------------------------------------
# 与改造前算法的等价性
# ---------------------------------------------------------------------------
def _legacy_select(combo: FakeCombo, mapping, current) -> None:
    """复刻 B4 之前 ``_make_combo`` / ``_combo`` 的选中算法（按映射下标）。"""
    for i, (_disp, val) in enumerate(mapping):
        if val == current:
            combo.setCurrentIndex(i)
            break


# 语料覆盖：正常映射、值不在映射里、空映射、值重复、非字符串值、单项映射
_MAPPING_CORPUS = [
    [("PNG", "png"), ("JPG", "jpg"), ("WebP", "webp")],
    [("自动选择", "auto"), ("OxiPNG", "oxipng"), ("Pillow", "pillow")],
    [("无", 0), ("Sub", 1), ("Up", 2), ("Average", 3), ("Paeth", 4), ("Mixed", 5)],
    [
        ("与源相同", "same"),
        ("PNG", "png"),
        ("JPG", "jpg"),
        ("WebP", "webp"),
        ("BMP", "bmp"),
        ("TIFF", "tiff"),
    ],
    [("唯一项", "only")],
    [("A", "x"), ("B", "x")],  # 值重复、文案不重复
    [],
]


@pytest.mark.parametrize("mapping", _MAPPING_CORPUS)
def test_select_matches_legacy_index_algorithm(mapping):
    """新旧选中算法在「无重复显示文案」的映射上必须逐条同结果。

    这里把每一个可能的 current（含一个不存在的值）都跑一遍。
    """
    candidates = [v for _d, v in mapping] + ["__not_in_mapping__", None]
    for current in candidates:
        new = _combo_from(mapping)
        gui_base.bind_combo_mapping(new, mapping)
        gui_base.select_combo_value(new, current)

        old = _combo_from(mapping)
        _legacy_select(old, mapping, current)

        assert new.currentIndex() == old.currentIndex(), (
            f"mapping={mapping} current={current!r}: "
            f"新算法选中 {new.currentIndex()}，旧算法选中 {old.currentIndex()}"
        )


def test_repopulate_preserves_logical_value_across_relabeling():
    """语言切换场景：候选项文案全变，但选中的**逻辑值**必须保住。

    这是 ``_repopulate_combo`` 的核心契约（切语言时 compress 页会调它两次）。
    """
    zh = [("与源相同", "same"), ("PNG", "png"), ("JPG", "jpg")]
    en = [("Same as source", "same"), ("PNG", "png"), ("JPG", "jpg")]

    combo = _combo_from(zh)
    gui_base.bind_combo_mapping(combo, zh)
    gui_base.select_combo_value(combo, "jpg")

    # 复刻 _repopulate_combo 的步骤
    current_val = gui_base.combo_value(combo)
    combo.clear()
    gui_base.bind_combo_mapping(combo, en)
    for disp, _v in en:
        combo.addItem(disp)
    gui_base.select_combo_value(combo, current_val)

    assert gui_base.combo_value(combo) == "jpg"
    assert combo.currentText() == "JPG"


def test_duplicate_display_text_is_the_only_known_divergence():
    """钉死唯一已知的新旧分叉点，防止它被误当成「已修复」。

    重复显示文案下 ``dict()`` 会折叠掉一项，新算法只能选到第一个同名项。
    本项目当前不存在这种映射（由 ``test_no_literal_mapping_has_duplicate_labels``
    保证），所以这条分叉不可达；这里把它显式记录下来，将来真要引入重复文案时，
    改的人能一眼看到代价。
    """
    mapping = [("同名", "first"), ("同名", "second")]
    new = _combo_from(mapping)
    gui_base.bind_combo_mapping(new, mapping)
    gui_base.select_combo_value(new, "second")

    old = _combo_from(mapping)
    _legacy_select(old, mapping, "second")

    assert old.currentIndex() == 1  # 旧算法按下标，选到第 2 项
    assert new.currentIndex() == 0  # 新算法按文案，只能选到第 1 项


# ---------------------------------------------------------------------------
# 源码静态守卫
# ---------------------------------------------------------------------------
def _load_locale(name: str) -> dict:
    return json.loads((_LOCALES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _literal_label(node: ast.AST, table: dict) -> str | None:
    """把 mapping 的「显示文案」表达式求值成字符串；求不出来返回 None。

    只认两种写法（覆盖本项目全部字面量 mapping）：
    ``"PNG"`` 这样的常量，和 ``tr("some.key")`` 这样的单参 tr 调用。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tr"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return table.get(node.args[0].value, f"<MISSING:{node.args[0].value}>")
    return None


_COMBO_BUILDERS = {"_combo", "_make_combo", "_repopulate_combo"}


def _iter_literal_mappings():
    """产出 ``(文件名, 行号, [显示文案表达式, ...])``，覆盖所有字面量 mapping。"""
    for path in sorted(_GUI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in _COMBO_BUILDERS:
                continue
            for arg in node.args:
                if not isinstance(arg, ast.List):
                    continue
                labels = [
                    e.elts[0] for e in arg.elts if isinstance(e, ast.Tuple) and len(e.elts) == 2
                ]
                if labels:
                    yield path.name, node.lineno, labels


def test_literal_mapping_scan_actually_found_something():
    """守卫的守卫：扫描器失效（比如写法变了）时必须报出来，而不是静默零命中。"""
    found = list(_iter_literal_mappings())
    assert len(found) >= 10, f"字面量 mapping 扫描只命中 {len(found)} 处，扫描器可能已失效"


@pytest.mark.parametrize("locale", _LOCALES)
def test_no_literal_mapping_has_duplicate_labels(locale):
    """三语下都不许出现「同一个下拉框里两个候选项文案相同」。

    这是 B4 换选中算法后唯一会导致行为分叉的输入形态（见上一条用例）。
    一旦有人加了重复文案，用户表现是「选了 A 却跳到 B」，且**不报任何错**。
    """
    table = _load_locale(locale)
    problems = []
    for fname, lineno, label_nodes in _iter_literal_mappings():
        labels = [_literal_label(n, table) for n in label_nodes]
        resolved = [x for x in labels if x is not None]
        dup = {x for x in resolved if resolved.count(x) > 1}
        if dup:
            problems.append(f"{fname}:{lineno} 重复文案 {sorted(dup)}")
    assert not problems, f"[{locale}] " + "; ".join(problems)


def test_mapping_private_attribute_never_leaks_outside_base():
    """ODD-07 回归守卫：除 gui/base.py 外，任何模块都不许再碰 ``combo._mapping``。

    穿透一旦复活，属性改名/换控件库时所有读取点会**静默**失效
    （PyQt 在槽里吞异常，界面表现为「下拉框选了没反应」）。
    """
    offenders = []
    for path in sorted(_GUI_DIR.glob("*.py")):
        if path.name == "base.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "_mapping":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, "私有属性穿透复活：" + ", ".join(offenders)


def test_public_combo_api_is_exported_from_base():
    """四个公开函数必须留在 gui/base 上（三个模块按名字 import 它们）。"""
    for name in ("bind_combo_mapping", "combo_mapping", "combo_value", "select_combo_value"):
        assert callable(getattr(gui_base, name, None)), f"gui.base 缺少 {name}"
