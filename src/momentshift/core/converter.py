"""Single-task ffmpeg execution with live progress reporting.

This module is deliberately **framework-agnostic** (no Qt). It reports progress
and logs through plain callbacks, so the queue manager can wire them to Qt
signals without coupling the engine to the UI.
"""

from __future__ import annotations

import subprocess
# Suppress the per-task console window on Windows (no cmd popups during batch runs).
WIN_SILENT = getattr(subprocess, "CREATE_NO_WINDOW", 0)
from pathlib import Path
from typing import Callable, Optional

from .presets import build_args
from .logger import get_logger

ProgressCallback = Callable[[int], None]
LogCallback = Callable[[str], None]

log = get_logger("converter")


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
    - ``returncode > 0`` => ffmpeg exited with an error (stderr captured).

    Any unexpected Python exception is caught, logged, and returned as a
    negative return code so it never crashes the host application.
    """
    try:
        args = build_args(task, hw)
        cmd = [ffmpeg_path, *args]
        log.info("ffmpeg command: %s", " ".join(cmd))
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("Failed to build ffmpeg arguments: %s", exc)
        return (-2, f"internal error building arguments: {exc}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge logs into stdout for deadlock-free reading
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            creationflags=WIN_SILENT,
        )
    except OSError as exc:
        log.error("Failed to launch ffmpeg: %s", exc)
        return (-1, f"failed to launch ffmpeg: {exc}")

    duration_ms: Optional[int] = None
    last_lines: list[str] = []
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
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
                last_lines.append(line)
                if len(last_lines) > 60:
                    last_lines.pop(0)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("Error while reading ffmpeg output: %s", exc)
        try:
            proc.kill()
        except OSError:
            pass
        return (-3, f"internal error reading ffmpeg output: {exc}")

    returncode = proc.wait()
    log.info(
        "ffmpeg finished: returncode=%s input=%s output=%s",
        returncode, task.input_path, task.output_path,
    )

    if returncode != 0:
        tail = "\n".join(last_lines[-30:])
        log.error("ffmpeg failed (rc=%s). Last output:\n%s", returncode, tail)
        return (returncode, tail or f"ffmpeg exited with code {returncode}")

    # Record the produced file size for the size-comparison UI.
    try:
        task.dst_size = Path(task.output_path).stat().st_size
    except OSError:
        task.dst_size = 0

    return (0, "")
