"""内置 FunASR 本地服务端（OpenAI 兼容，供其他应用调用）。

v0.8.9：服务模式语义纠正——不是「连别处部署的服务器」，而是**本软件自己
作为服务器**，监听 ``http://127.0.0.1:8000/v1``，供其他应用（浏览器/脚本/
第三方工具）调用本地 ASR 推理。客户端与服务端的角色与 v0.8.3 理解完全相反。

端点（与用户本地 C:\\FunASR\\server.py 同款 API）：
- ``POST /v1/audio/transcriptions``：multipart ``file``（视频/音频）+ ``model``
  → ``{"text": "..."}``（长音频自动 60s 分段后汇总）
- ``GET /v1/models``：可用模型列表
- ``GET /health``：健康检查

实现：纯标准库 ``http.server``（项目无 Flask 依赖），multipart 手写解析
（Python 3.13 已移除 cgi 模块）。推理复用 ``core.funasr_engine`` 的本地引擎
（进程内单例缓存）；音频统一经 ffmpeg 归一化为 16k 单声道 wav。
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .asr_worker import (
    build_extract_audio_cmd,
    build_segment_cut_cmd,
    probe_duration,
    probe_duration_ffmpeg,
    segment_ranges,
)
from .ffmpeg import find_ffmpeg, find_ffprobe
from .funasr.utils.wav_io import load_wav
from .funasr_engine import DEFAULT_MODEL_ID, MODEL_CATALOG, transcribe_local
from .logger import get_logger

log = get_logger("asr_server")

SEGMENT_SEC = 60.0  # 服务端长音频分段长度（与客户端一致）

_BOUNDARY_RE = re.compile(r'boundary="?([^";]+)"?')


def parse_multipart(content_type: str, body: bytes) -> dict:
    """解析 multipart/form-data（标准库手写实现，替代已移除的 cgi 模块）。

    Returns:
        ``{字段名: 值}``；文件字段的值为 ``(原始文件名, bytes)``。
    """
    m = _BOUNDARY_RE.search(content_type or "")
    if not m:
        return {}
    boundary = ("--" + m.group(1)).encode("utf-8")
    result: dict = {}
    for part in body.split(boundary):
        part = part.strip(b"\r\n")
        if not part or part in (b"--",):
            continue
        sep = part.find(b"\r\n\r\n")
        if sep < 0:
            continue
        head = part[:sep].decode("utf-8", "replace")
        data = part[sep + 4 :]
        if data.endswith(b"\r\n"):
            data = data[:-2]
        name_m = re.search(r'name="([^"]*)"', head)
        fn_m = re.search(r'filename="([^"]*)"', head)
        if name_m is None:
            continue
        key = name_m.group(1)
        if fn_m:
            result[key] = (fn_m.group(1), data)
        else:
            result[key] = data.decode("utf-8", "replace")
    return result


class AsrServer:
    """本地 ASR HTTP 服务端（后台线程运行，OpenAI 兼容）。

    v0.8.10：新增可选 ``api_key`` —— 设置后所有请求必须带
    ``Authorization: Bearer <api_key>``（对齐用户 C:\\FunASR\\server.py 的
    ASR_API_KEY 行为）；留空不校验。
    """

    def __init__(self, log_cb=None, api_key: str = ""):
        self._log_cb = log_cb or (lambda line: None)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._port = 8000
        self._model = DEFAULT_MODEL_ID
        self._api_key = api_key or ""
        self._lock = threading.Lock()

    # -- 生命周期 -----------------------------------------------------
    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}/v1"

    def start(
        self,
        port: int = 8000,
        model: str = DEFAULT_MODEL_ID,
        api_key: str | None = None,
    ) -> tuple[bool, str]:
        """在后台线程启动 HTTP 服务。返回 (是否成功, 消息)。

        ``api_key`` 为 None 时保持构造时值；传字符串（含空串）则更新鉴权密钥。
        """
        with self._lock:
            if self._httpd is not None:
                return False, "服务已在运行"
            self._port = int(port)
            self._model = model or DEFAULT_MODEL_ID
            if api_key is not None:
                self._api_key = api_key or ""
            try:
                self._httpd = ThreadingHTTPServer(("127.0.0.1", self._port), self._make_handler())
            except OSError as exc:
                self._httpd = None
                self._log_cb(f"[服务] 启动失败：{exc}")
                return False, f"端口 {port} 被占用或不可用：{exc}"
            self._thread = threading.Thread(
                target=self._httpd.serve_forever, daemon=True, name="asr-http-server"
            )
            self._thread.start()
            self._log_cb(f"[服务] 已启动：{self.url}（模型 {self._model}）")
            return True, f"服务已启动：{self.url}"

    def stop(self) -> None:
        with self._lock:
            httpd, self._httpd = self._httpd, None
            if httpd is not None:
                try:
                    httpd.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                httpd.server_close()
                self._log_cb("[服务] 已停止")
        self._thread = None

    # -- 内部 ---------------------------------------------------------
    def _make_handler(self):
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # 静默默认访问日志
                pass

            def _send_json(self, obj, status: int = 200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth_ok(self) -> bool:
                """可选 Bearer 鉴权（v0.8.10）；未配置 api_key 时放行。"""
                if not server._api_key:
                    return True
                auth = self.headers.get("Authorization", "")
                return auth == f"Bearer {server._api_key}"

            def do_GET(self):
                if not self._auth_ok():
                    self._send_json({"error": {"message": "invalid api key"}}, 401)
                    return
                if self.path.rstrip("/") == "/health":
                    self._send_json({"status": "ok", "model": server._model})
                elif self.path.rstrip("/") == "/v1/models":
                    data = [
                        {
                            "id": s["id"],
                            "object": "model",
                            "owned_by": "momentshift-local",
                        }
                        for s in MODEL_CATALOG
                        if s.get("kind") == "asr" and s.get("engine")
                    ]
                    self._send_json({"object": "list", "data": data})
                else:
                    self._send_json({"error": {"message": "not found"}}, 404)

            def do_POST(self):
                if self.path.rstrip("/") != "/v1/audio/transcriptions":
                    self._send_json({"error": {"message": "not found"}}, 404)
                    return
                if not self._auth_ok():
                    self._send_json({"error": {"message": "invalid api key"}}, 401)
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if length <= 0:
                    self._send_json({"error": {"message": "empty body"}}, 400)
                    return
                body = self.rfile.read(length)
                fields = parse_multipart(self.headers.get("Content-Type", ""), body)
                file_item = fields.get("file") or fields.get("audio")
                if not file_item:
                    self._send_json({"error": {"message": "missing file field"}}, 400)
                    return
                fname, data = file_item
                model = fields.get("model") or server._model
                server._log_cb(f"[服务] 收到转写请求：{fname or '(未知)'}（模型 {model}）")
                try:
                    text = server.transcribe_bytes(fname or "audio", data, model)
                except Exception as exc:  # noqa: BLE001 - 转写失败返回错误信息
                    server._log_cb(f"[服务] 转写失败：{exc}")
                    self._send_json({"error": {"message": str(exc)}}, 500)
                    return
                server._log_cb(f"[服务] 转写完成（{len(text)} 字）")
                self._send_json({"text": text})

        return _Handler

    def transcribe_bytes(self, fname: str, data: bytes, model: str) -> str:
        """处理一次上传：临时文件 → 归一化 16k wav → 分段 → 推理 → 汇总。"""
        tmpdir = Path(tempfile.mkdtemp(prefix="ms_asr_srv_"))
        try:
            src = tmpdir / Path(fname).name
            src.write_bytes(data)
            wav = self._normalize_wav(src, tmpdir)
            return self._transcribe_wav(wav, model, tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _normalize_wav(self, src: Path, tmpdir: Path) -> Path:
        """把任意输入归一化为 16k 单声道 wav；已是标准 wav 则直接用。"""
        if src.suffix.lower() == ".wav":
            try:
                load_wav(str(src))  # 校验 16k 单声道 PCM
                return src
            except Exception:
                pass  # 非 16k/多声道 → 转码
        ffmpeg = find_ffmpeg("") or "ffmpeg"
        out = tmpdir / "norm.wav"
        cmd = build_extract_audio_cmd(ffmpeg, str(src), str(out))
        import subprocess

        proc = subprocess.run(cmd, capture_output=True, timeout=600)
        if proc.returncode != 0 or not out.is_file():
            raise RuntimeError("音频归一化失败（ffmpeg）")
        return out

    def _transcribe_wav(self, wav: Path, model: str, tmpdir: Path) -> str:
        """16k wav → 60s 分段转写 → 汇总文本。"""
        ffmpeg = find_ffmpeg("") or "ffmpeg"
        ffprobe = find_ffprobe()
        duration = probe_duration(ffprobe, str(wav)) or probe_duration_ffmpeg(ffmpeg, str(wav))
        ranges = segment_ranges(duration or 0.0, SEGMENT_SEC)
        if len(ranges) <= 1:
            return transcribe_local(str(wav), model_id=model)
        parts: list[str] = []
        for i, (start, end) in enumerate(ranges):
            seg = tmpdir / f"seg_{i}.wav"
            cmd = build_segment_cut_cmd(ffmpeg, str(wav), str(seg), start, end)
            import subprocess

            proc = subprocess.run(cmd, capture_output=True, timeout=300)
            if proc.returncode != 0 or not seg.is_file():
                log.warning("分段 %d 切割失败", i)
                continue
            parts.append(transcribe_local(str(seg), model_id=model))
        return "".join(parts)
