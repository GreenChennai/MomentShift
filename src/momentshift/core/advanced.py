"""高级参数（按 image / video / audio 分类），对应 UI 的「高级设置」面板。

职责边界：
- 做：维护高级参数的默认值与当前值、入队时做深拷贝快照、把参数翻译成 ffmpeg 参数。
- 不做：不渲染界面（面板在 gui/advanced_panel）；不执行 ffmpeg。

依赖：core/config、core/ffmpeg、core/platform；被依赖：core/presets、core/queue、gui/advanced_panel。

这些参数直接映射到 ffmpeg 命令行（借鉴 FFmpegFreeUI 的思路：给用户具体的高层
旋钮，而不是裸 flag）。每个分类有自己的子面板：

- image：输出质量（默认无损；有损目标格式给一个质量滑块）
- video：分辨率 / 帧率 / 视频码率 / 编码器，外加「合并为一个文件」
- audio：音频码率 / 采样率 / 声道，外加「合并为一个文件」

线程模型（v0.8.0 RISK-03）
--------------------------
``adv`` 是一个模块级可变字典：GUI 线程（高级设置面板）写，转换线程池读。
v0.8.0 之前两侧完全没有同步，且工作线程读的是**实时**值——用户在任务跑到一半
时改一下分辨率，正在排队的任务会跟着变，属于典型的数据竞争 + 行为不可预测。

现在的约定是：

1. ``adv`` 只允许 GUI 线程写（面板是唯一写入方）；
2. 入队时由 :func:`snapshot` 做一次**深拷贝**，快照存进 ``Task.adv``；
3. 工作线程只读自己那份快照，永远不碰 ``adv``。

深拷贝是必须的：``image`` 分类下嵌了 ``compress`` 子字典，v0.8.0 之前
``dict(advanced.get(...))`` 只是浅拷贝，子字典仍与全局共享引用，改压缩参数照样
会串台。

``_LOCK`` 保护的是"读到半个写完的 dict"这种撕裂，不是业务级原子性——真正让行为
可预测的是入队快照。

**v0.8.0 显式设计决策（Q3）**：高级参数在**入队时**定格。入队之后再改面板，
只影响之后新入队的任务，不影响已在队列里的任务。这是一处**可观察的行为变更**。
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path

# 下拉框里可选的分辨率 / 帧率 / 码率预设。
# "original" 表示「不下发该参数，交给 ffmpeg 默认处理（copy / auto）」。
RESOLUTIONS = ["original", "3840x2160", "1920x1080", "1280x720", "854x480"]
FPS_OPTIONS = ["original", "60", "30", "25", "24"]
VIDEO_BITRATES = ["original", "20M", "10M", "5M", "2M", "1M"]
AUDIO_BITRATES = ["original", "320k", "256k", "192k", "128k"]
CODECS = ["original", "H.264", "H.265", "copy"]
SAMPLE_RATES = ["original", "48000", "44100"]
CHANNELS = ["original", "stereo", "mono"]

# 「仅提取音频」的可选音频格式（v0.8.3）。MP3 必须排第一（产品要求）。
AUDIO_FORMATS = ["mp3", "wav", "flac", "aac", "m4a", "ogg"]


def probe_video_size(path: str) -> tuple[int, int] | None:
    """用 ffprobe 探测视频分辨率。

    Args:
        path: 视频文件路径。
    Returns:
        ``(宽, 高)``；ffprobe 缺失、超时或输出无法解析时返回 ``None``。
    """
    import shutil
    import subprocess

    from .config import cfg
    from .ffmpeg import find_ffmpeg
    from .platform import run_silent

    try:
        ffmpeg = find_ffmpeg(cfg.ffmpegSource.value)
        if not ffmpeg:
            return None
        ffprobe = shutil.which("ffprobe") or str(Path(ffmpeg).parent / "ffprobe.exe")
        if not ffprobe or not Path(ffprobe).is_file():
            return None
        proc = run_silent(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=s=x:p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        line = proc.stdout.strip()
        if "x" in line:
            w, h = line.split("x")
            return int(w), int(h)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # 静默原因：分辨率解析失败则回退默认 None
    return None


def ffmpeg_param_defaults(kind: str) -> dict:
    """取某类媒体的 FFmpeg 压缩参数默认值（V0.8.21 新增）。

    Args:
        kind: ``video`` / ``audio`` / ``image``。
    Returns:
        ``{参数键: 默认值}`` 的新字典，键即 ``ff_v_*`` / ``ff_a_*`` / ``ff_i_*``。
    Notes:
        默认值从 :mod:`core.ffmpeg_compress` 的参数规格表现取，不在这里另抄
        一份——两处硬编码迟早会漂移。延迟导入避免 core 内部循环依赖。
    """
    from .ffmpeg_compress import PARAMS_BY_KIND

    specs = PARAMS_BY_KIND.get(kind, {})
    return {key: spec.get("default") for key, spec in specs.items()}


def default_options() -> dict:
    """返回一份全新的、覆盖所有分类的高级参数默认值。

    Returns:
        每次调用都是**新建**的嵌套字典，调用方可以随意就地修改而不会污染默认值。
    """
    return {
        "image": {
            # 输出质量 1..100，越大越好。默认给满 100：宁可文件大，也不要用户
            # 在不知情的情况下损失画质，需要更小体积时再由用户主动下调。
            "quality": 100,
            "lossless": True,  # png 保持无损
            # 分格式的质量覆盖值，None 表示沿用上面的通用 quality
            "png_quality": None,
            "jpg_quality": None,
            "webp_quality": None,
            # 压缩后端配置，恒为字典。backend="auto" 时按格式路由
            # （png→oxipng / jpg→jpegoptim / gif→gifsicle / 其他→pillow）。
            "compress": {
                "backend": "auto",
                "quality": 95,
                # oxipng（「自动选择」默认：最优参数 / 无损 / 中等速度 / 优化全开）
                # 元数据默认保留（strip=none、jo_strip=none）：EXIF 里的拍摄
                # 信息一旦被剥掉就找不回来，宁可多占几 KB
                "level": 3,
                "interlace": True,
                "strip": "none",
                "filter": 0,
                "zc": 6,
                "alpha": False,
                # jpegoptim
                "jo_mode": "lossless",
                "jo_max": 85,
                "jo_strip": "none",
                "jo_progressive": "auto",
                "jo_threshold": 0,
                "jo_preserve": True,
                "jo_retry": False,
                # pillow
                "pil_quality": 95,
                "pil_optimize": True,
                "pil_progressive": True,
                "pil_subsampling": "4:4:4",
                # v0.8.22：图片「压缩程序」新增 FFmpeg 后端，参数默认值从
                # ffmpeg_compress 规格表现取，避免在此另抄一份。
                **ffmpeg_param_defaults("image"),
            },
            "compress_mode": "lossless",
        },
        # V0.8.21：视频/音频的高级设置改为「转换编排 + FFmpeg 压缩」两段式。
        # 原先那套 resolution / fps / bitrate / codec / crf 与「压缩」大模块里
        # 的 FFmpeg 参数是两套并行且能力更弱的旋钮，用户要压体积得跑两个界面。
        # 现在只保留**编排类**开关（合并 / 仅提取音频 / 音频格式），画质与体积
        # 统一交给 compress 子字典里的 ff_* 参数，在转换完成后的压缩阶段生效。
        "video": {
            "merge": False,
            # v0.8.3：「仅提取音频」——启用后不再转换视频画面，只输出音频。
            "extract_audio": False,
            "audio_format": "mp3",
            "compress": ffmpeg_param_defaults("video"),
        },
        "audio": {
            "merge": False,
            "compress": ffmpeg_param_defaults("audio"),
        },
    }


# 实时可变的高级参数（GUI 面板写入这里；入队时由 snapshot 拷走）。
adv: dict = default_options()

# 保护 ``adv`` 的读写不被撕裂。用 RLock 是因为 snapshot() 内部会调 get()。
_LOCK = threading.RLock()


def _reset_into(target: dict, defaults: dict) -> None:
    """把 ``defaults`` **原地**写回 ``target``，尽量保留已有子字典的对象标识。

    递归到最里层逐键赋值，而不是 ``target.clear(); target.update(...)``：
    高级设置面板里到处是 ``adv = advanced.adv["image"]`` /
    ``comp = advanced.adv["image"]["compress"]`` 这种"抓住子字典再原地改"的写法，
    只要重建了子字典，面板手上那份就成了跟全局脱钩的孤儿，改了也不生效。
    """
    for key in [k for k in target if k not in defaults]:
        del target[key]
    for key, value in defaults.items():
        current = target.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            _reset_into(current, value)
        else:
            target[key] = value


def reset() -> None:
    """把所有分类恢复成默认值（原地，不重新绑定 ``adv``）。"""
    defaults = default_options()
    with _LOCK:
        _reset_into(adv, defaults)


def get(category: str) -> dict:
    """返回某分类的**实时**参数字典（可直接原地修改）。

    仅供 GUI 线程使用（高级设置面板需要拿到可写引用）。工作线程要读参数请用
    ``Task.adv``——那是入队时的快照，见模块文档的线程模型说明。
    """
    with _LOCK:
        return adv.get(category, {})


def snapshot(category: str) -> dict:
    """返回某分类参数的**深拷贝**，用于入队时定格（v0.8.0 Q3）。

    深拷贝而非 ``dict(...)``：``image`` 下面还嵌着 ``compress`` 子字典，浅拷贝
    会让已入队任务和面板共享同一个子字典，用户改压缩参数会串到在跑的任务上。
    """
    with _LOCK:
        return copy.deepcopy(adv.get(category, {}))


def is_merge_enabled(category: str) -> bool:
    with _LOCK:
        return bool(adv.get(category, {}).get("merge", False))


def is_extract_audio_enabled(category: str) -> bool:
    """v0.8.3：视频分类是否启用了「仅提取音频」。

    该开关只对视频分类有意义；其他分类一律返回 False。
    """
    if category != "video":
        return False
    with _LOCK:
        return bool(adv.get("video", {}).get("extract_audio", False))


def build_advanced_args(category: str, target: str, options: dict | None = None) -> list[str]:
    """按 ``category``/``target`` 生成额外的 ffmpeg 参数。

    ``options`` 省略时退化为该分类的实时快照（正常调用链上 ``presets.build_args``
    总会显式传入 ``Task.adv``，这条兜底只在手工构造 Task 的测试里走到）。
    返回的列表由 ``presets.build_args`` 插进最终命令行。
    """
    if options is None:
        options = snapshot(category)
    if not options:
        return []

    extra: list[str] = []

    if category == "image":
        quality = int(options.get("quality", 95))
        if target == "jpg":
            jpg_q = options.get("jpg_quality")
            if jpg_q is not None:
                quality = int(jpg_q)
            # mjpeg 的 -q:v 是「越小越好」：1 最佳、31 最差，与界面上
            # 「质量越高越好」的直觉相反，所以这里把 1~100 反向映射到 31~2
            q = max(2, min(31, round(31 - quality / 100 * 29)))
            extra += ["-q:v", str(q)]
        elif target == "webp":
            webp_q = options.get("webp_quality")
            if webp_q is not None:
                quality = int(webp_q)
            extra += ["-quality", str(quality)]
        elif target == "png":
            png_q = options.get("png_quality")
            if png_q is not None:
                quality = int(png_q)
            # PNG 本身无损，「质量」在这里只能落到压缩级别 0~9 上，
            # 换的是体积和耗时，不影响画质
            lvl = max(0, min(9, round(quality / 100 * 9)))
            extra += ["-compression_level", str(lvl)]
        elif target == "tiff":
            extra += ["-compression_algo", "deflate"]
        # bmp 无可调参数，故意不加任何编码选项

    # V0.8.21：video / audio 不再在**转换阶段**下发画质参数。
    #
    # 这两类的高级设置已改为 FFmpeg 压缩参数（存在 options["compress"] 里的
    # ff_v_* / ff_a_*），由「转换 → 压缩」两段式的第二段
    # （core.queue.compress_after_conversion）执行，走 ffmpeg_compress.build_args。
    # 转换阶段保持预设表的原样输出，避免同一个旋钮在两处各拧一半：
    # 早期实现同时下发 -c:v 与 -b:v，再叠加压缩阶段的 -crf，实际结果取决于
    # 谁最后覆盖谁，用户完全无法预期。
    #
    # 这里刻意不返回任何东西，而不是删掉分支——留着这段注释，免得后来者
    # 看到 video/audio 没有处理就"顺手补上"，把两段式又搅回一段。

    return extra


def build_merge_args(
    category: str, input_paths: list[str], output_path: str, options: dict | None = None
) -> list[str]:
    """构造把 ``input_paths`` 合并成 ``output_path`` 的 concat 命令。

    视频走带音轨的 concat filter；纯音频用 ``v=0:a=1``。
    """
    if options is None:
        options = snapshot(category)
    n = len(input_paths)
    cmd = ["-hide_banner", "-nostats", "-y"]
    for p in input_paths:
        cmd += ["-i", p]

    if category == "video":
        # 拼出形如 [0:v][0:a][1:v][1:a]...concat=n=N:v=1:a=1[outv][outa] 的滤镜图
        ins = "".join(f"[{i}:v][{i}:a]" for i in range(n))
        cmd += [
            "-filter_complex",
            f"{ins}concat=n={n}:v=1:a=1[outv][outa]",
            "-map",
            "[outv]",
            "-map",
            "[outa]",
            "-c:v",
            "libx264",
            "-crf",
            str(options.get("crf", 18)),
            "-preset",
            "slow",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]
    else:  # 音频：只拼接音轨
        ins = "".join(f"[{i}:a]" for i in range(n))
        cmd += [
            "-filter_complex",
            f"{ins}concat=n={n}:v=0:a=1[outa]",
            "-map",
            "[outa]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]

    cmd.append(output_path)
    return cmd


def get_current_args(category: str, target: str = "") -> list[str]:
    """按当前实时参数生成 ffmpeg 参数（预览用，不参与实际入队）。"""
    return build_advanced_args(category, target, snapshot(category))
