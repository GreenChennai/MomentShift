"""Lightweight data model for a single conversion task.

Kept free of any Qt/UI dependency so the conversion engine stays testable in
isolation (and swappable behind a service layer later).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Task:
    """One file to convert. ``status`` is one of the constants below."""

    PENDING = "pending"
    RUNNING = "running"
    COMPRESSING = "compressing"  # v0.6.8：转换完成，正在压缩
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"

    id: str
    input_path: str
    output_path: str
    target_format: str  # profile key, e.g. "jpg", "mp4"
    category: str       # source category: "image" | "audio" | "video"
    use_gpu: bool

    status: str = PENDING
    progress: int = 0    # 0..100
    error: str = ""
    duration_ms: int = 0
    src_size: int = 0    # source file size in bytes (filled at enqueue)
    dst_size: int = 0    # output file size in bytes (filled after conversion)

    # Advanced conversion options (see core/advanced.py). ``None`` => use defaults.
    adv: dict = None
    # v0.6.0: 用户是否启用了高级设置（控制转换后压缩）
    compress_enabled: bool = False
    compress_progress: int = 0  # 压缩阶段进度 0..100
    compress_done: bool = False  # 压缩完成
    pre_compress_size: int = 0  # v0.6.7：压缩前 dst_size（用于对比）
    # Merge mode: when True, ``input_paths`` (not just ``input_path``) are
    # concatenated into a single output (used for "merge into one file").
    merge: bool = False
    input_paths: list = None
