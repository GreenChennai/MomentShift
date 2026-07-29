"""Single-task ffmpeg execution with live progress reporting.

This module is deliberately **framework-agnostic** (no Qt). It reports progress
and logs through plain callbacks, so the queue manager can wire them to Qt
signals without coupling the engine to the UI.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Optional

from .presets import build_args

ProgressCallback = Callable[[int], None]
LogCallback = Callable[[str], None]


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort terminate/kill of a running ffmpeg process."""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
    except OSError:
        pass


def run_conversion(
    task,
    ffmpeg_path: str,
    hw: Optional[dict] = None,
    on_progress: Optional[ProgressCallback] = None,
    on_log: Optional[LogCallback] = None,
    cancel_event: Optional[object] = None,
) -> tuple[Optional[int], str]:
    """Run one conversion. Returns ``(returncode, error_text)``.

    - ``returncode == 0`` => success.
    - ``returncode is None`` => canceled via ``cancel_event``.
    - ``returncode < 0`` => failed to even launch ffmpeg.
    """
    args = build_args(task, hw)
    cmd = [ffmpeg_path, *args]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge logs into stdout for deadlock-free reading
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return (-1, f"failed to launch ffmpeg: {exc}")

    duration_ms: Optional[int] = None
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _terminate(proc)
            return (None, "canceled")

        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        # ffmpeg -progress lines are `key=value` with no leading whitespace.
        if "=" in line and not line.startswith(" "):
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key == "duration_ms" and val.isdigit():
                duration_ms = int(val)
                task.duration_ms = duration_ms
            elif key == "out_time_ms" and val.isdigit() and duration_ms:
                pct = min(100, int(int(val) / duration_ms * 100))
                if on_progress:
                    on_progress(pct)
            elif key == "progress" and val == "end":
                if on_progress:
                    on_progress(100)
        else:
            if on_log:
                on_log(line)

    returncode = proc.wait()
    return (returncode, "")
