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
