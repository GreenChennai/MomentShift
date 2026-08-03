"""C1 文案闸门：三语语言包一致性守卫（改文案之前先把「不许改坏」钉死）。

背景
----
批次 C 要把 ``zh_CN.json`` 566 条文案整体口语化重写，再同步 ``zh_TW`` / ``en_US``。
这种「三个文件手工对着改」的活儿，最典型的翻车方式有四种，而且四种全都**不报错**、
只在用户界面上默默显示错东西：

1. **键集合漂移**：中文加了一条新 key，繁中/英文忘了加。运行期 ``tr()`` 会静默回退到
   ``en_US``，再回退到裸 key —— 英文用户看到中文、或者直接看到 ``convert.start``。
2. **占位符对不上**：中文写 ``已完成 {n} 个``，英文写成 ``{count} done``。
   ``str.format(n=3)`` 抛 ``KeyError``，``translator.get`` 把它吞掉并原样返回文案，
   用户界面上就出现一个赤裸的 ``{count}``。
3. **key 拼错 / 改名漏改**：代码里 ``tr("convert.btn.start")`` 而 JSON 里叫
   ``convert.start``，界面显示裸 key。反过来，JSON 里堆着 200 条谁都不用的死 key，
   翻译时白白花三倍人力。
4. **空文案**：某个语言的值被改成空串，界面上那块位置直接消失，比显示错字更难发现。

所以在动 566 条文案之前，先用本脚本把这四类问题变成「提交前必然红灯」。

四层断言
--------
1. **键集合严格相等**：三语 key 集合两两全等（不是子集，是全等）。
2. **占位符逐键一致**：用 ``string.Formatter`` 的真实语义（而不是正则）解析
   ``{}`` / ``{name}`` / ``{name:>8}`` / ``{{`` 转义，逐 key 断言三语占位符
   **名字集合完全一致**；顺便断言没有花括号不配对导致的解析异常。
3. **ast 静态扫描**（不是正则，正则会把注释和文档字符串里的 ``tr(...)`` 也算进来）：
   - 3a 代码里 ``tr("字面量")`` 用到的 key，必须三语都存在 —— 失败。
   - 3b 调用点传的占位符实参，必须与文案里的占位符**完全一致**；
     ``tr("k").format(x=1)`` 这种「先取文案再 format」的写法也认。—— 失败。
   - 3c 反向：JSON 里存在但代码里从未引用的 key —— **只警告不失败**
     （key 可能被动态拼接引用，静态扫描无法百分百判死，误杀比漏报代价大）。
4. **空值检测**：
   - 4a 同一个 key 在部分语言为空、另一些语言非空 —— 失败（典型的漏翻）。
   - 4b 三语全空的 key 必须登记在 ``INTENTIONALLY_BLANK`` 白名单里 —— 否则失败
     （「故意留白」必须是有人签字的决定，不能是手滑）。

用法::

    python tests/i18n_coverage.py            # 不需要 PyQt6，也不需要 Qt 离屏环境
    python tests/i18n_coverage.py --verbose  # 额外列出全部未引用 key

退出码 0 表示四层断言全过（警告不影响退出码）。
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from string import Formatter

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from momentshift.i18n.translator import LOCALE_DIR, SUPPORTED_LOCALES  # noqa: E402

# 以简体中文为基准语言：批次 C 的规则是「以中文为准，同步繁中与英文」。
REFERENCE_LOCALE = "zh_CN"

# 扫描哪些目录里的 Python 代码。tools/ 里有历史文案批量脚本，tests/ 里有引用 key 的
# 冒烟脚本，都算「引用方」，一并纳入，避免把它们用到的 key 误判成死 key。
SCAN_ROOTS = ("src", "tools", "tests")

# ``tr(key, default=None, **kwargs)`` —— default 是 tr() 自己的形参，不是占位符实参。
TR_RESERVED_KWARGS = frozenset({"default"})

# 故意留白的文案：队列 / 暂存区的空态提示标签，设计上就只显示图标不显示文字。
# 三语同时为空是有意为之；任何新增的全空 key 都必须先加进这里，否则第 4 层断言会红。
INTENTIONALLY_BLANK = frozenset(
    {
        "convert.queue.empty",
        "convert.staging.empty",
        "upscale.compare.empty",
        "upscale.queue.empty",
        "upscale.queue.hint",
        "upscale.staging.empty",
    }
)

_FAILURES: list[str] = []
_WARNINGS: list[str] = []

_FORMATTER = Formatter()


# =============================================================================
# 小工具
# =============================================================================
def check(cond: bool, msg: str) -> None:
    """断言并把失败累积起来，一次性汇报（比首个失败即退出更好定位）。"""
    if cond:
        print(f"  [OK]   {msg}", flush=True)
    else:
        print(f"  [FAIL] {msg}", flush=True)
        _FAILURES.append(msg)


def warn(msg: str) -> None:
    """记一条警告：会打印、会计数，但不影响退出码。"""
    print(f"  [WARN] {msg}", flush=True)
    _WARNINGS.append(msg)


class PlaceholderError(ValueError):
    """文案里的花括号不配对 / 格式说明非法。"""


def placeholders(text: str) -> frozenset[str]:
    """取出一段文案里全部占位符的**名字**。

    Args:
        text: 文案原文。

    Returns:
        占位符名字集合。``{0}`` 这类位置占位符返回 ``"0"``；``{}`` 自动编号返回 ``""``；
        ``{a.b}`` / ``{a[0]}`` 只取根名字 ``a``（``str.format`` 也是按根名字取实参的）。

    Raises:
        PlaceholderError: 花括号不配对等 ``str.format`` 本身就会拒绝的写法。

    Notes:
        刻意用 ``string.Formatter().parse()`` 而不是正则：它就是 ``str.format`` 用的
        同一套解析器，``{{`` 转义、``{n:>8}`` 格式说明、``{v!r}`` 转换标记全部天然正确，
        自己写正则一定会在这些边角上和运行期行为分叉。
    """
    names: set[str] = set()
    try:
        for _literal, field, _spec, _conv in _FORMATTER.parse(text):
            if field is None:
                continue
            names.add(field.split(".")[0].split("[")[0])
    except ValueError as exc:
        raise PlaceholderError(str(exc)) from exc
    return frozenset(names)


# =============================================================================
# ast 扫描
# =============================================================================
@dataclass(frozen=True)
class TrCall:
    """一个 ``tr()`` 调用点。"""

    file: str
    line: int
    key: str | None  # None 表示 key 是动态表达式，静态无法判定
    fills: frozenset[str]  # 调用点提供的占位符实参名
    opaque_fills: bool  # True 表示用了 **kwargs / 位置实参，实参名不可知


def _format_wrapper_fills(tree: ast.AST) -> dict[int, tuple[frozenset[str], bool]]:
    """找出 ``tr(...).format(...)` 形态，把外层 format 的实参记到内层 tr 调用上。

    Args:
        tree: 单个模块的 ast。

    Returns:
        ``id(tr 调用节点) -> (实参名集合, 是否含不可知实参)``。

    Notes:
        项目里存在 ``tr('compress.done.by').format(backend=name)`` 这种写法。
        它同样正确地填了占位符，如果不认这一形态，第 3b 层会给出假阳性。
    """
    found: dict[int, tuple[frozenset[str], bool]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "format"):
            continue
        inner = func.value
        if not (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "tr"
        ):
            continue
        names = frozenset(kw.arg for kw in node.keywords if kw.arg)
        opaque = any(kw.arg is None for kw in node.keywords) or bool(node.args)
        found[id(inner)] = (names, opaque)
    return found


def scan_sources() -> tuple[list[TrCall], set[str]]:
    """扫描全部 Python 源码，收集 ``tr()`` 调用点与所有字符串字面量。

    Returns:
        ``(tr 调用点列表, 源码里出现过的全部字符串字面量集合)``。
        字面量集合含 f-string 的静态片段，用于第 3c 层判断「这个 key 可能是被动态拼的」。
    """
    self_path = Path(__file__).resolve()
    calls: list[TrCall] = []
    literals: set[str] = set()

    for root_name in SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            # 本脚本自己写着一堆 key 字面量（白名单），扫进去会把它们误判成「被引用」
            if path.resolve() == self_path:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:  # pragma: no cover - 语法错早就被 ruff 拦了
                _FAILURES.append(f"{path} 解析失败：{exc}")
                continue

            wrappers = _format_wrapper_fills(tree)
            rel = path.relative_to(REPO_ROOT).as_posix()

            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    literals.add(node.value)
                elif isinstance(node, ast.JoinedStr):
                    for part in node.values:
                        if isinstance(part, ast.Constant) and isinstance(part.value, str):
                            literals.add(part.value)

                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "tr"
                    and node.args
                ):
                    continue

                first = node.args[0]
                key = (
                    first.value
                    if (isinstance(first, ast.Constant) and isinstance(first.value, str))
                    else None
                )
                fills = set(kw.arg for kw in node.keywords if kw.arg)
                opaque = any(kw.arg is None for kw in node.keywords)
                extra = wrappers.get(id(node))
                if extra is not None:
                    fills |= set(extra[0])
                    opaque = opaque or extra[1]
                calls.append(TrCall(rel, node.lineno, key, frozenset(fills), opaque))

    return calls, literals


def _dynamic_prefixes(literals: set[str]) -> set[str]:
    """从字符串字面量里挑出「可能被用来拼 key 的命名空间前缀」。

    Args:
        literals: 源码里全部字符串字面量（含 f-string 静态片段）。

    Returns:
        以 ``.`` 结尾、长度 >= 4 且不含空格的字面量集合，例如 ``engine.help.``。

    Notes:
        ``attach_help(row, f"engine.help.{p.key}")`` 这类拼接让静态扫描看不到完整 key。
        把这类前缀提出来，凡是落在前缀下的 key 都不再报「未引用」，宁可漏报不要误杀。
    """
    return {s for s in literals if s.endswith(".") and len(s) >= 4 and " " not in s}


# =============================================================================
# 主流程
# =============================================================================
def load_locales() -> dict[str, dict[str, str]]:
    """按 ``SUPPORTED_LOCALES`` 读取全部语言包（不复用 Translator，避免它吞异常）。"""
    data: dict[str, dict[str, str]] = {}
    for loc in SUPPORTED_LOCALES:
        path = LOCALE_DIR / f"{loc.value}.json"
        data[loc.value] = json.loads(path.read_text(encoding="utf-8"))
    return data


def layer1_key_sets(data: dict[str, dict[str, str]]) -> set[str]:
    """第 1 层：三语 key 集合严格相等。"""
    print("\n[第 1 层] 三语 key 集合是否严格相等", flush=True)
    base = set(data[REFERENCE_LOCALE])
    check(bool(base), f"基准语言 {REFERENCE_LOCALE} 非空（{len(base)} 条）")
    for name, obj in data.items():
        if name == REFERENCE_LOCALE:
            continue
        cur = set(obj)
        missing = sorted(base - cur)
        extra = sorted(cur - base)
        check(not missing, f"{name}: 无缺失 key（缺失 {len(missing)} 条{_peek(missing)}）")
        check(not extra, f"{name}: 无多余 key（多余 {len(extra)} 条{_peek(extra)}）")
        check(len(cur) == len(base), f"{name}: 条目数 {len(cur)} == 基准 {len(base)}")
    return base


def _peek(items: list[str], limit: int = 8) -> str:
    """把超长清单截断成一句可读的提示。"""
    if not items:
        return ""
    head = "、".join(items[:limit])
    tail = " …" if len(items) > limit else ""
    return f"：{head}{tail}"


def layer2_placeholders(
    data: dict[str, dict[str, str]], keys: set[str]
) -> dict[str, frozenset[str]]:
    """第 2 层：占位符逐键一致，并顺带校验花括号合法性。

    Returns:
        ``key -> 基准语言的占位符集合``，供第 3b 层复用。
    """
    print("\n[第 2 层] 占位符是否逐键三语一致", flush=True)
    parse_errors: list[str] = []
    per_key: dict[str, dict[str, frozenset[str]]] = {}

    for name, obj in data.items():
        for key, text in obj.items():
            try:
                per_key.setdefault(key, {})[name] = placeholders(text)
            except PlaceholderError as exc:
                parse_errors.append(f"{name}/{key}: {exc}（原文 {text!r}）")

    check(not parse_errors, f"花括号全部合法可被 str.format 解析（异常 {len(parse_errors)} 条）")
    for line in parse_errors:
        print(f"         - {line}", flush=True)

    mismatched: list[str] = []
    for key in sorted(keys):
        got = per_key.get(key, {})
        if len(set(got.values())) > 1:
            detail = "；".join(f"{n}={sorted(v) or '无'}" for n, v in sorted(got.items()))
            mismatched.append(f"{key} → {detail}")
    check(not mismatched, f"三语占位符名字集合完全一致（不一致 {len(mismatched)} 条）")
    for line in mismatched:
        print(f"         - {line}", flush=True)

    ref = {k: per_key.get(k, {}).get(REFERENCE_LOCALE, frozenset()) for k in keys}
    with_ph = sorted(k for k, v in ref.items() if v)
    print(f"  [i]    含占位符的文案 {len(with_ph)} 条", flush=True)
    return ref


def layer3_static_scan(keys: set[str], ref_ph: dict[str, frozenset[str]], verbose: bool) -> None:
    """第 3 层：ast 静态扫描代码与文案的双向一致性。"""
    print("\n[第 3 层] ast 静态扫描：代码 ↔ 文案 双向一致", flush=True)
    calls, literals = scan_sources()
    literal_calls = [c for c in calls if c.key is not None]
    dynamic_calls = [c for c in calls if c.key is None]
    print(
        f"  [i]    扫描 {len(SCAN_ROOTS)} 个源码目录，共 {len(calls)} 处 tr() 调用"
        f"（字面量 key {len(literal_calls)} 处 / 动态 key {len(dynamic_calls)} 处）",
        flush=True,
    )

    # -- 3a 代码用到的 key 必须存在 --
    unknown = sorted({f"{c.key}  ←  {c.file}:{c.line}" for c in literal_calls if c.key not in keys})
    check(not unknown, f'代码里 tr("字面量") 用到的 key 三语均存在（缺失 {len(unknown)} 处）')
    for line in unknown:
        print(f"         - {line}", flush=True)

    # -- 3b 调用点实参必须与占位符完全一致 --
    bad_fills: list[str] = []
    for call in literal_calls:
        if call.key not in keys or call.opaque_fills:
            continue
        needed = ref_ph.get(call.key, frozenset())
        # default 是 tr() 自己的形参；只有文案真的写了 {default} 时才当占位符看
        given = call.fills - (TR_RESERVED_KWARGS - needed)
        if given != needed:
            bad_fills.append(
                f"{call.file}:{call.line} tr({call.key!r}) 需要 {sorted(needed) or '无'}"
                f"，实际传入 {sorted(given) or '无'}"
            )
    check(not bad_fills, f"tr() 调用点占位符实参与文案完全匹配（不匹配 {len(bad_fills)} 处）")
    for line in bad_fills:
        print(f"         - {line}", flush=True)

    # -- 3c 反向：未被引用的 key（只警告） --
    prefixes = _dynamic_prefixes(literals)
    referenced = {c.key for c in literal_calls if c.key}
    unused = sorted(
        k
        for k in keys
        if k not in referenced and k not in literals and not any(k.startswith(p) for p in prefixes)
    )
    if unused:
        warn(
            f"{len(unused)} 条 key 在代码中从未被引用（可能是历史遗留死文案，"
            f"翻译时可考虑先清理；静态扫描无法判死，故只警告）"
        )
        buckets = Counter(k.rsplit(".", 1)[0] for k in unused)
        for ns, cnt in buckets.most_common(12):
            print(f"         · {ns}.* × {cnt}", flush=True)
        if len(buckets) > 12:
            print(f"         · …… 另有 {len(buckets) - 12} 个命名空间", flush=True)
        if verbose:
            print("         全部未引用 key：", flush=True)
            for i in range(0, len(unused), 4):
                print("           " + "  ".join(unused[i : i + 4]), flush=True)
        else:
            print("         （加 --verbose 查看完整清单）", flush=True)
    else:
        check(True, "不存在从未被引用的 key")


def layer4_empty_values(data: dict[str, dict[str, str]], keys: set[str]) -> None:
    """第 4 层：空值检测。"""
    print("\n[第 4 层] 空文案检测", flush=True)
    partial: list[str] = []
    all_blank: list[str] = []
    for key in sorted(keys):
        # 只看「这个语言里确实有这条 key」的情况：key 缺失是第 1 层的职责，
        # 在这里当成空值会给同一个问题报两遍，模糊焦点。
        present = {n: obj[key] for n, obj in data.items() if key in obj}
        blanks = [n for n, text in present.items() if not text.strip()]
        if not blanks:
            continue
        if len(blanks) == len(present):
            all_blank.append(key)
        else:
            filled = [n for n in present if n not in blanks]
            partial.append(f"{key} → 空：{'、'.join(blanks)}；非空：{'、'.join(filled)}")

    check(not partial, f"不存在「部分语言为空、部分语言有文案」的漏翻（{len(partial)} 条）")
    for line in partial:
        print(f"         - {line}", flush=True)

    unregistered = sorted(k for k in all_blank if k not in INTENTIONALLY_BLANK)
    check(
        not unregistered,
        f"三语全空的 key 均已登记在 INTENTIONALLY_BLANK（未登记 {len(unregistered)} 条"
        f"{_peek(unregistered)}）",
    )

    stale = sorted(k for k in INTENTIONALLY_BLANK if k not in all_blank)
    if stale:
        warn(f"INTENTIONALLY_BLANK 里有 {len(stale)} 条已不再是全空，白名单该瘦身了{_peek(stale)}")
    registered = len(all_blank) - len(unregistered)
    print(f"  [i]    三语全空 {len(all_blank)} 条，其中已登记 {registered} 条", flush=True)


def main(argv: list[str]) -> int:
    """跑完四层断言并汇总。"""
    verbose = "--verbose" in argv
    data = load_locales()
    names = "、".join(data)
    print(f"[i18n] 语言包目录：{LOCALE_DIR}", flush=True)
    print(f"[i18n] 参与校验的语言：{names}（基准 {REFERENCE_LOCALE}）", flush=True)
    for name, obj in data.items():
        print(f"  [i]    {name}.json：{len(obj)} 条", flush=True)

    keys = layer1_key_sets(data)
    ref_ph = layer2_placeholders(data, keys)
    layer3_static_scan(keys, ref_ph, verbose)
    layer4_empty_values(data, keys)

    print(flush=True)
    if _FAILURES:
        print(f"[i18n] 失败 {len(_FAILURES)} 项：", flush=True)
        for msg in _FAILURES:
            print(f"  - {msg}", flush=True)
        sys.stdout.flush()
        return 1
    tail = f"，警告 {len(_WARNINGS)} 项（不阻断）" if _WARNINGS else ""
    print(
        f"[i18n] 全部通过：{len(keys)} 条文案 × {len(data)} 种语言，四层断言无一失守{tail}",
        flush=True,
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
