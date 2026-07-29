"""Engine verification without a real ffmpeg.

- Simulates ffmpeg output via a fake ``Popen`` so we exercise the real
  progress-parsing + size-capture + worker-signal path (the code that was
  previously crashing on conversion).
- Runs the ConversionManager in an offscreen QApplication (no GUI widgets are
  created, so it avoids the sandbox's render-kill) to confirm the threaded
  worker -> signal -> manager flow does not raise.
"""

import sys
import tempfile
from pathlib import Path

os_environ = __import__("os").environ
os_environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from momentshift.core import converter as converter_mod
from momentshift.core.queue import ConversionManager
from momentshift.core.models import Task


class FakePopen:
    """Mimics ffmpeg: emits progress lines then creates the output file."""

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.out = cmd[-1]
        self._lines = iter([
            "ffmpeg version fake",
            "duration_ms=1000",
            "out_time_ms=500",
            "progress=continue",
            "out_time_ms=1000",
            "progress=end",
        ])
        self.stdout = self

    def readline(self):
        try:
            return next(self._lines) + "\n"
        except StopIteration:
            return ""

    def wait(self):
        try:
            Path(self.out).write_bytes(b"x" * 2048)
        except OSError:
            pass
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def test_converter_parse():
    converter_mod.subprocess.Popen = FakePopen
    from momentshift.core.converter import run_conversion

    task = Task(
        id="t1", input_path="in.png", output_path="out.jpg",
        target_format="jpg", category="image", use_gpu=False,
    )
    logs = []
    rc, err = run_conversion(task, "fake", {}, on_log=logs.append, on_progress=lambda p: None)
    assert rc == 0, f"expected rc 0, got {rc}: {err}"
    assert task.dst_size == 2048, task.dst_size
    # on_log receives non-progress ffmpeg output lines (e.g. the banner).
    assert any("ffmpeg version" in l for l in logs), "ffmpeg output should be logged"
    print("[engine] converter parse + size capture OK", flush=True)


def test_manager_flow():
    converter_mod.subprocess.Popen = FakePopen
    app = QApplication(sys.argv)

    tmp = Path(tempfile.mkdtemp())
    src = tmp / "photo.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n")  # a valid-enough PNG header

    mgr = ConversionManager()
    mgr.ffmpeg_path = "fake"
    mgr.hw = {}

    added, skipped = mgr.add_files([str(src)], "jpg", None, False)
    assert added and not skipped, (added, skipped)
    assert added[0].src_size == len(src.read_bytes()), "src_size should be recorded"
    assert mgr.start(), "start() should launch with a ffmpeg path set"

    deadline = __import__("time").time() + 10
    while __import__("time").time() < deadline:
        app.processEvents()
        if not mgr.is_running:
            break
        __import__("time").sleep(0.05)

    task = mgr.get_task(added[0].id)
    assert task.status == Task.DONE, task.status
    assert task.dst_size == 2048, task.dst_size
    print("[engine] manager worker->signal->DONE flow OK", flush=True)


if __name__ == "__main__":
    test_converter_parse()
    test_manager_flow()
    print("ENGINE TESTS PASSED", flush=True)
