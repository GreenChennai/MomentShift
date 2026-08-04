"""FunASR OpenAI 兼容服务的 HTTP 客户端（纯标准库 ``urllib``）。

职责边界：
- 做：封装 ``/health`` 健康检查与 ``/v1/audio/transcriptions`` 转写请求，
  以 multipart 上传音频文件、解析 ``{"text": ...}`` 响应；所有失败路径抛出
  带人话的 :class:`AsrError`。
- 不做：不管理线程；不做分段/切分（在 ``core/asr_worker``）。

为什么用标准库而不是 ``requests``：MomentShift 是 PyInstaller 独立应用，仓库
里没有 HTTP 依赖（requirements/pyproject 无 requests/httpx）。新增第三方依赖
会复杂化打包链，而这里只用到 POST multipart + GET，urllib 完全够用。

服务端契约（见用户部署的 ``C:\\FunASR\\server.py``，OpenAI 兼容）：
- ``POST <base_url>/audio/transcriptions``，multipart 字段 ``file``（可加
  ``model``）；响应 ``{"text": "..."}``。
- ``GET <base_url>/models``。
- ``GET /health``（注意在应用根路径，不是 ``/v1`` 下）。
- 鉴权：服务端设了 ``ASR_API_KEY`` 时需要 ``Authorization: Bearer <key>``，
  未设则任何请求都放行 —— 因此客户端 ``api_key`` 为空时**不带**请求头。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# 单段转写默认超时（秒）。Paraformer 冷启动首次推理可到 ~17s（DML 着色器编译），
# 热推理 8s 音频约 1.1s；120s 音频约 9.4s。给足余量避免误报超时。
DEFAULT_TIMEOUT = 120.0
# 健康检查超时（秒）：只是探活，给短一点，连不上立刻反馈。
HEALTH_TIMEOUT = 5.0

# 零配置默认服务三件套（与用户本地部署的 ``C:\\FunASR\\server.py`` 一致）：
# 未启用「服务模式」时界面就用这一套。config.py 的 asrBaseUrl / asrModel
# 默认值也引用这里，保证「界面默认」与「配置默认」永远同源。
DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
DEFAULT_MODEL = "paraformer-zh"


class AsrError(RuntimeError):
    """ASR 请求失败；``message`` 为可直接展示给用户的人话。"""


# 中日韩统一表意文字（含扩展 A / 兼容区），用于 CJK 间空格清理。
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.match(ch))


def clean_cjk_spaces(text: str) -> str:
    """清理「两个 CJK 字符之间」的空格（Paraformer 输出按字加空格）。

    服务端已做过同样的处理，这里保留一份保守实现：只删除左右都是 CJK 的
    空格，绝不碰拉丁字母 / 数字 / 标点两侧的空格，因此对非 CJK 文本是
    幂等无操作的。
    """
    chars = list(text or "")
    out: list[str] = []
    for i, ch in enumerate(chars):
        if ch == " " and 0 < i < len(chars) - 1:
            if _is_cjk(chars[i - 1]) and _is_cjk(chars[i + 1]):
                continue
        out.append(ch)
    return "".join(out)


def _health_url(base_url: str) -> str:
    """从 base_url 推导健康检查地址。

    默认 base_url 形如 ``http://127.0.0.1:8000/v1``，而服务端把 ``/health``
    挂在应用根路径（``/v1`` 之外）。这里统一剥掉结尾的 ``/v1`` 再拼，
    兼容「base_url 不含 /v1」的自定义地址。
    """
    base = (base_url or "").rstrip("/")
    if base.endswith("/v1"):
        return base[:-3] + "/health"
    return base + "/health"


def asr_health(base_url: str, timeout: float = HEALTH_TIMEOUT) -> tuple[bool, str]:
    """健康检查。

    Args:
        base_url: 服务地址（形如 ``http://127.0.0.1:8000/v1``）。
        timeout: 超时秒数。

    Returns:
        ``(ok, 消息)``：``ok=True`` 时消息为服务返回的状态文本；失败时消息为
        可读原因。**不抛异常**——健康检查的语义就是「问一句，答不上来就给
        用户看原因」，异常留给真正需要失败的 :func:`transcribe`。
    """
    url = _health_url(base_url)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status == 200:
                return True, "ok"
            return False, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except TimeoutError:
        return False, "timeout"
    except urllib.error.URLError as exc:
        reason = exc.reason
        return False, str(reason) if reason else "connection error"
    except OSError as exc:
        return False, str(exc)


def _build_multipart(wav_path: str, model: str, api_key: str) -> tuple[bytes, dict[str, str]]:
    """构造 multipart/form-data 请求体与请求头。

    Args:
        wav_path: 要上传的音频文件路径。
        model: 模型名（OpenAI 兼容字段；当前服务端固定 ``paraformer-zh``）。
        api_key: 可选鉴权 key；为空则不带头（服务端未开启鉴权时放行）。

    Returns:
        ``(body, headers)``。
    """
    boundary = "----MomentShift" + uuid.uuid4().hex
    filename = Path(wav_path).name

    parts: list[bytes] = []
    # model 字段（OpenAI 兼容要求，服务端当前忽略但保留协议兼容性）
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\n'
        f"{model}\r\n".encode()
    )
    # file 字段
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    with open(wav_path, "rb") as fh:
        parts.append(fh.read())
    parts.append(f"\r\n--{boundary}--\r\n".encode())

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return b"".join(parts), headers


def transcribe(
    base_url: str,
    model: str,
    api_key: str,
    wav_path: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """把一段音频提交给 ASR 服务，返回识别文本。

    Args:
        base_url: 服务地址（形如 ``http://127.0.0.1:8000/v1``）。
        model: 模型名。
        api_key: 可选鉴权 key；为空则不带头。
        wav_path: 本地音频文件路径。
        timeout: 单次请求超时秒数。

    Returns:
        识别出的纯文本（已清理 CJK 间空格）。

    Raises:
        AsrError: 文件不存在、连接失败、超时、非 200、响应非法或缺 ``text``
            字段时抛出，``message`` 为可直接展示的人话。
    """
    path = Path(wav_path)
    if not path.is_file():
        raise AsrError(f"音频文件不存在：{wav_path}")

    url = (base_url or "").rstrip("/") + "/audio/transcriptions"
    body, headers = _build_multipart(str(path), model or "", api_key or "")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise AsrError(f"服务返回错误码 {exc.code}") from exc
    except TimeoutError:
        raise AsrError(f"请求超时（{timeout:.0f} 秒）") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        detail = str(reason) if reason else ""
        raise AsrError(f"无法连接 ASR 服务：{detail or '连接被拒绝'}") from exc
    except json.JSONDecodeError as exc:
        raise AsrError("服务响应不是有效的 JSON") from exc

    if not isinstance(payload, dict):
        raise AsrError("服务响应格式异常（缺少 text 字段）")
    text = payload.get("text")
    if text is None:
        raise AsrError("服务响应缺少 text 字段")
    return clean_cjk_spaces(str(text))
