"""R5 风险闸门：证明「删掉散落的 qconfig.save() 之后配置一项都不会丢」。

背景
----
v0.8.0 之前配置持久化是「双保险」：``main_window._connect_config`` 遍历
``dir(cfg.__class__)`` 把所有 ``ConfigItem.valueChanged`` 连到 ``qconfig.save()``，
同时业务代码里还散落着 10 多处手写 ``qconfig.save()``。ODD-22 要求收敛成单一策略
（只保留集中式）。

风险在于：如果 ``dir()`` 扫描漏掉某个类型的配置项（例如 ``RangeConfigItem`` 或基
类继承来的项），删掉散落调用后那一项就会**静默丢失**——用户改了设置，重启没了，
而且没有任何报错。

所以在动手删之前，先用本脚本把「覆盖完整」这件事钉死。

三层断言
--------
1. **接管完整性**：``connect_autosave()`` 返回的名单必须与 ``iter_config_items()``
   枚举出的全部配置项**完全相等**（不是子集，是全等）。
2. **信号真的会触发存盘**：逐项写入一个与当前值不同的合法新值，断言存盘函数被调
   到。这一步能抓住「连上了但信号不发」（例如 validator 把新值判回原值）的情况。
3. **真的能落盘并读回**：把配置存到临时文件，再用一个全新的 ``QConfig`` 实例从磁
   盘加载，逐项断言值与写入的一致——这才是用户口中的「重启还在」。

用法::

    PYTHONPATH=src .venv/Scripts/python.exe tests/config_coverage.py

退出码 0 表示三层断言全过。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from PyQt6.QtGui import QColor
from qfluentwidgets import (
    ColorConfigItem,
    ConfigItem,
    OptionsConfigItem,
    RangeConfigItem,
    qconfig,
)

from momentshift.core import config as cfg_mod
from momentshift.core.config import cfg, connect_autosave, iter_config_items

_FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    """断言并把失败累积起来，一次性汇报（比首个失败即退出更好定位）。"""
    if cond:
        print(f"  [OK]   {msg}")
    else:
        print(f"  [FAIL] {msg}")
        _FAILURES.append(msg)


def same_value(expected: Any, actual: Any) -> bool:
    """比较两个配置值，吸收掉与「是否被持久化」无关的表示差异。

    Notes:
        - ``FolderValidator`` 会把 ``\\`` 归一化成 ``/``，这是它的正常行为，不算丢值。
        - ``QColor`` 不能直接跟字符串比，统一取 ``name()``。
    """
    if isinstance(expected, QColor) or isinstance(actual, QColor):
        left = expected.name() if isinstance(expected, QColor) else str(expected)
        right = actual.name() if isinstance(actual, QColor) else str(actual)
        return left.lower() == right.lower()
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.replace("\\", "/") == actual.replace("\\", "/")
    return bool(expected == actual)


def probe_value(name: str, item: ConfigItem, tmp_dir: Path) -> Any:
    """为配置项造一个「与当前值不同的合法新值」。

    Args:
        name: 配置项属性名，仅用于报错信息。
        item: 配置项本体。
        tmp_dir: 可用于 ``FolderValidator`` 类配置项的真实临时目录。

    Returns:
        可以安全写入的新值。

    Raises:
        RuntimeError: 无法为该配置项构造探针值（说明本脚本需要扩充分支）。
    """
    current = item.value

    if isinstance(item, RangeConfigItem):
        low, high = item.validator.min, item.validator.max
        return high if current != high else low

    if isinstance(item, ColorConfigItem):
        return QColor("#ff00ff") if current.name().lower() != "#ff00ff" else QColor("#00ff00")

    if isinstance(item, OptionsConfigItem):
        for opt in item.validator.options:
            if opt != current:
                return opt
        raise RuntimeError(f"{name}: 只有一个可选值，无法构造不同的探针值")

    if isinstance(current, bool):
        return not current

    if isinstance(current, int):
        return current + 1

    if isinstance(current, list):
        return [*current, "MomentShift Probe Font"]

    if isinstance(current, str):
        # 目录类配置项必须给真实存在的路径，否则 FolderValidator 会把它改回默认值
        if "Folder" in type(item.validator).__name__:
            probe = tmp_dir / f"probe_{name}"
            probe.mkdir(parents=True, exist_ok=True)
            return str(probe)
        return f"{current}_probe"

    raise RuntimeError(f"{name}: 未知类型 {type(current).__name__}，请扩充 probe_value")


def main() -> int:
    all_items = iter_config_items()
    names = [n for n, _ in all_items]
    print(f"[配置覆盖] Config 上共枚举到 {len(all_items)} 个配置项：")
    print("  " + ", ".join(names))

    # 本脚本会往配置项里写探针值。先把真实配置文件路径挪开，杜绝任何一次意外的
    # save() 把探针值糊到用户的 config.json 上。
    real_file = cfg.file

    # ---------------- 第 1 层：接管完整性 ----------------
    print("\n[第 1 层] connect_autosave() 是否接管了全部配置项")
    connected = connect_autosave()
    missing = sorted(set(names) - set(connected))
    extra = sorted(set(connected) - set(names))
    check(not missing, f"无漏接项（漏接：{missing or '无'}）")
    check(not extra, f"无多余项（多余：{extra or '无'}）")
    check(len(connected) == len(names), f"接管数量 {len(connected)} == 枚举数量 {len(names)}")
    check(connect_autosave() == connected, "重复调用 connect_autosave() 幂等，不会重复连接")

    # ---------------- 第 2 层：改值确实触发存盘 ----------------
    print("\n[第 2 层] 逐项改值是否都会触发存盘")
    saved: list[int] = []
    real_save = cfg_mod.qconfig.save

    def counting_save() -> None:
        saved.append(1)

    with tempfile.TemporaryDirectory() as td:
        tmp_dir = Path(td)
        cfg.file = tmp_dir / "probe_config.json"
        written: dict[str, Any] = {}

        cfg_mod.qconfig.save = counting_save  # type: ignore[method-assign]
        try:
            for name, item in all_items:
                before = len(saved)
                try:
                    new_value = probe_value(name, item, tmp_dir)
                except RuntimeError as exc:
                    check(False, str(exc))
                    continue
                item.value = new_value
                fired = len(saved) > before
                landed = same_value(new_value, item.value)
                check(fired, f"{name}: 改值触发了存盘（{before} → {len(saved)}）")
                check(landed, f"{name}: 新值被接受（期望 {new_value!r}，实际 {item.value!r}）")
                if landed:
                    written[name] = item.value
        finally:
            cfg_mod.qconfig.save = real_save  # type: ignore[method-assign]

        # ---------------- 第 3 层：落盘 + 冷读回 ----------------
        # ConfigItem 是类属性、全进程共享，没法真造一个「干净实例」。所以这里模拟
        # 一次完整的重启：存盘 → 把内存值全部打回默认值 → 从磁盘重新 load。
        # 如果哪一项没被 toDict() 序列化出去，打回默认后就再也读不回来。
        print("\n[第 3 层] 存盘 → 内存清零 → 从磁盘重新加载（等价于「重启还在」）")
        target = tmp_dir / "roundtrip.json"
        cfg.file = target
        qconfig.save()
        check(target.exists(), f"配置文件已写出：{target.name}")

        raw = json.loads(target.read_text(encoding="utf-8"))
        check(bool(raw), f"配置文件内容非空（{len(raw)} 个分组）")

        # 打回默认值同样会触发自动存盘。此刻 cfg.file 正指向 target，如果不挪开，
        # 这一轮「清零」会把默认值反手写回 target，把刚存进去的探针值全冲掉——
        # 后面的冷读回就变成「读回默认值」，断言必然全挂。
        # 这里刻意不 disconnect，而是把落点挪到废纸篓：连接关系保持原样，冷读回
        # 走的才是真实的信号链路。
        cfg.file = tmp_dir / "scratch.json"
        for _, item in all_items:
            item.value = item.defaultValue
        check(
            not same_value(written["outputSuffix"], cfg.outputSuffix.value),
            "内存值已成功打回默认（确认这次冷读回不是假阳性）",
        )
        check(
            json.loads(target.read_text(encoding="utf-8")) == raw,
            "清零过程没有污染 target（探针值仍完整留在磁盘上）",
        )

        qconfig.load(str(target), cfg)
        for name, expected in written.items():
            got = getattr(cfg, name).value
            check(
                same_value(expected, got), f"{name}: 冷读回一致（期望 {expected!r}，实际 {got!r}）"
            )

        cfg.file = real_file

    # ---------------- 汇总 ----------------
    print()
    if _FAILURES:
        print(f"[配置覆盖] 失败 {len(_FAILURES)} 项：")
        for msg in _FAILURES:
            print(f"  - {msg}")
        return 1
    print(f"[配置覆盖] 全部通过：{len(all_items)} 个配置项，三层断言无一失守")
    print("[配置覆盖] 结论：可以安全删除业务代码里散落的 qconfig.save()")
    return 0


if __name__ == "__main__":
    # 配置项的 valueChanged 是 Qt 信号，需要一个 QApplication 才能可靠投递
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication(sys.argv)
    raise SystemExit(main())
