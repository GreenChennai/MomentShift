"""v0.8.1 回归：GPU 探测与真实转码参数一致 + 动图转静态图单帧约束。

覆盖（不依赖真实 ffmpeg，纯参数级断言）：
- Bug2b：``hardware._probe_encoder`` 拼入的探测命令必须与
  ``presets._gpu_video_args`` 的**视频编码部分**逐项一致 —— 否则会出现
  「探测用裸 ``-c:v`` 通过、真实转码带 ``-rc vbr_quality -qv 23`` 时 ffmpeg
  构建不认 ``-qv`` 而直接失败且无 CPU 回退」的错位。
- Bug2a：``presets.build_args`` 对静态图片目标追加 ``-frames:v 1``（动图转
  静态图只取首帧），GIF / 视频 / 音频目标不受影响。
"""

from __future__ import annotations

import subprocess

import pytest

from momentshift.core import hardware as hw_mod
from momentshift.core import presets as presets_mod
from momentshift.core.hardware import _probe_encoder
from momentshift.core.models import Task
from momentshift.core.presets import _gpu_video_args, _gpu_video_encode_args, build_args


def _task(target_format: str, category: str, src: str = "a.gif") -> Task:
    """构造一个最小 Task：只关心 build_args 用到的字段。"""
    return Task(
        id="x",
        input_path=src,
        output_path=f"out.{target_format}",
        target_format=target_format,
        category=category,
        use_gpu=False,
    )


# ---------------------------------------------------------------------------
# Bug2b：探测参数 == 实际视频编码参数
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "encoder",
    [
        "h264_nvenc",
        "hevc_nvenc",
        "h264_qsv",
        "hevc_qsv",
        "h264_amf",
        "hevc_amf",
        "h264_videotoolbox",
        "h264_v4l2m2m",
    ],
)
def test_gpu_encode_args_cover_all_families(encoder: str) -> None:
    """每个编码器家族都要有对应的质量旋钮，不能落到兜底分支之外。"""
    args = _gpu_video_encode_args(encoder)
    assert args[0] == "-c:v" and args[1] == encoder
    assert len(args) >= 4, f"{encoder} 缺少质量旋钮: {args}"


@pytest.mark.parametrize("encoder", ["h264_amf", "h264_nvenc", "h264_qsv", "h264_videotoolbox"])
def test_probe_uses_same_encode_args_as_real_conversion(encoder: str) -> None:
    """探测命令必须包含与真实转码完全一致的视频编码参数（Bug2b 的核心契约）。

    用假 ``run_silent`` 拦截探测命令：断言命令里出现的视频编码参数与
    ``_gpu_video_args(encoder, target)`` 的视频编码前缀逐项一致。
    """
    captured: dict[str, list[str]] = {}

    def _fake_run_silent(cmd, **kwargs):
        captured["cmd"] = cmd
        proc = subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        return proc

    original = hw_mod.run_silent
    hw_mod.run_silent = _fake_run_silent
    try:
        assert _probe_encoder("ffmpeg", encoder) is True
    finally:
        hw_mod.run_silent = original

    cmd = captured["cmd"]
    real = _gpu_video_args(encoder, "mp4")
    encode_prefix = _gpu_video_encode_args(encoder)

    # 真实转码参数里，视频编码部分必须是探测参数的前缀（探测==实际）
    assert real[: len(encode_prefix)] == encode_prefix
    # 探测命令里必须原样出现这套视频编码参数
    assert _in_cmd(cmd, encode_prefix), (
        f"{encoder} 探测参数与真实转码不一致:\n  probe={cmd}\n  real={real}"
    )


def _in_cmd(cmd: list[str], wanted: list[str]) -> bool:
    """``wanted`` 是否作为连续子序列出现在 ``cmd`` 里。"""
    for i in range(len(cmd) - len(wanted) + 1):
        if cmd[i : i + len(wanted)] == wanted:
            return True
    return False


def test_probe_returns_false_when_encoder_param_unsupported() -> None:
    """探测失败（参数不被支持）时返回 False，从而走 CPU 回退。"""
    captured: dict[str, list[str]] = {}

    def _fake_run_silent(cmd, **kwargs):
        captured["cmd"] = cmd
        proc = subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Unrecognized option 'qv'.")
        return proc

    original = hw_mod.run_silent
    hw_mod.run_silent = _fake_run_silent
    try:
        assert _probe_encoder("ffmpeg", "h264_amf") is False
    finally:
        hw_mod.run_silent = original

    # 即便失败，探测命令也必须带质量旋钮 —— 这正是要暴露的错位
    assert "-qv" in captured["cmd"] or "-cq" in captured["cmd"] or "-global_quality" in captured["cmd"]


# ---------------------------------------------------------------------------
# Bug2a：静态图片目标强制单帧
# ---------------------------------------------------------------------------
def test_gif_to_png_forces_single_frame() -> None:
    """GIF(动图) → PNG：必须追加 ``-frames:v 1``，取首帧。"""
    args = build_args(_task("png", "image"))
    assert args[-3:-1] == ["-frames:v", "1"], args


@pytest.mark.parametrize(
    ("target", "src"),
    [
        ("jpg", "a.png"),
        ("webp", "a.png"),
        ("bmp", "a.gif"),
        ("tiff", "a.png"),
    ],
)
def test_static_image_targets_force_single_frame(target: str, src: str) -> None:
    """所有静态图片目标都追加单帧约束；静态图互转（单帧输入）无害。"""
    args = build_args(_task(target, "image", src))
    assert "-frames:v" in args and "1" in args


@pytest.mark.parametrize(
    ("target", "category", "src"),
    [
        ("gif", "image", "a.png"),  # 图片源 → GIF：动图要保留多帧
        ("gif", "video", "a.mp4"),  # 视频 → GIF：调色板管线，保留多帧
        ("mp4", "video", "a.mp4"),  # 视频 → 视频
        ("mp3", "audio", "a.wav"),  # 音频
    ],
)
def test_animated_and_audio_targets_keep_all_frames(target: str, category: str, src: str) -> None:
    """GIF/视频/音频目标**不得**追加 ``-frames:v 1``。"""
    args = build_args(_task(target, category, src))
    assert "-frames:v" not in args, args
