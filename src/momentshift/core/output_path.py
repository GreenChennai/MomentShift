"""用户输出文件的落盘路径解析。

职责边界：
- 做：按「输出到源目录 / 输出到固定目录」两种模式拼出目标路径，并在重名时
  追加 ``_1`` / ``_2`` 序号；顺带把目标目录建出来。
- 不做：不判断该用什么扩展名（压缩看目标格式、放大看是不是静态图，规则各不
  相同，留在各自界面里），不写文件。

依赖：仅标准库；被依赖：gui.compress_interface、gui.upscale_interface。

历史背景（DUP-01）：v0.8.0 之前压缩与放大各有一份 ``_out_path``，除了扩展名
那两行以外逐字符相同。
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["unique_output_path"]

# 序号上限。正常情况下撞几次名就够了，设个上限是为了避免目录被外部程序刷屏
# 时（比如同步盘不断重建文件）在这里空转。
_MAX_DEDUP_TRIES = 10_000


def unique_output_path(
    src: str,
    *,
    ext: str,
    output_mode: str,
    suffix: str = "",
    folder: str = "",
) -> str:
    """算出 ``src`` 的输出路径，必要时补序号避开已存在的文件。

    Args:
        src: 源文件路径。
        ext: 目标扩展名，**带点**（``'.png'``）。
        output_mode: ``'same'`` 表示输出到源文件所在目录并给文件名加 ``suffix``；
            其他值表示输出到 ``folder``（``folder`` 为空时退回源目录）。
        suffix: ``output_mode == 'same'`` 时追加到文件名后的后缀，如 ``'_min'``。
        folder: 固定输出目录。
    Returns:
        目标文件的绝对/相对路径字符串（跟随输入的形式）。
    Raises:
        OSError: 目标目录建不出来（磁盘只读、路径非法、盘符不存在等）。
    Notes:
        重名检测是「看一眼文件在不在」，因此**必须串行调用**：两条任务并发问
        同一个问题会同时得到「不在」，然后一起往同一个路径写。队列侧的做法是
        把它放进 ``TaskPool`` 的 ``prepare_fn`` 钩子里，由池保证串行。
    """
    source = Path(src)
    if output_mode == "same":
        out_dir = source.parent
        stem = source.stem + (suffix or "")
    else:
        out_dir = Path(folder) if folder else source.parent
        stem = source.stem

    out_dir.mkdir(parents=True, exist_ok=True)
    candidate = out_dir / f"{stem}{ext}"
    index = 1
    while candidate.exists() and index <= _MAX_DEDUP_TRIES:
        candidate = out_dir / f"{stem}_{index}{ext}"
        index += 1
    return str(candidate)
