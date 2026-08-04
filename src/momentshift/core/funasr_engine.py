"""FunASR 本地推理引擎：模型管理 + 转写封装。

职责边界：
- 做：定位 ``tools/funasr/`` 下的模型、检测模型是否就绪、懒加载 Paraformer
  推理器（进程内单例，同用户 server.py 的 ``_model`` 模式）、把 16k wav
  转成中文文本（含 CJK 空格清理）。
- 不做：不下载模型（``core/funasr_download``）；不弹界面（``gui/asr_interface``）。

模型不随软件分发：下载目标目录 ``tools/funasr/<model-id>/``（``tools/`` 已在
.gitignore，模型绝不进 repo / 不打包）。引擎与用户 FunASR 部署共用同一套
Paraformer ONNX 模型，CPU int8 配置 ≈13x 实时。
"""

from __future__ import annotations

import threading
from pathlib import Path

from .asr_client import clean_cjk_spaces
from .platform import tools_dir

DEFAULT_MODEL_ID = "paraformer-large"
DEFAULT_THREADS = 8

# 模型清单（单一事实来源，UI / 下载 / 检测共用）。
# - ``engine`` 为 True 的模型可被转写流程自动选用（fsmn-vad 目前仅下载预留）。
# - ``files`` 里的 ``urls`` 按优先级排列，下载时逐个尝试。
MODEL_CATALOG: list[dict] = [
    {
        "id": "paraformer-large",
        "name_key": "asr.model.paraformer_large.name",
        "desc_key": "asr.model.paraformer_large.desc",
        "quantize": True,
        "optional": False,
        "engine": True,
        "size_mb": 238,
        "files": [
            {
                "name": "config.yaml",
                "size": 56799,
                "urls": [
                    "https://huggingface.co/funasr/Paraformer-large/resolve/main/config.yaml"
                ],
            },
            {
                "name": "am.mvn",
                "size": 11203,
                "urls": [
                    "https://huggingface.co/funasr/Paraformer-large/resolve/main/am.mvn"
                ],
            },
            {
                "name": "model_quant.onnx",
                "size": 238380216,
                "urls": [
                    "https://huggingface.co/funasr/Paraformer-large/resolve/main/model_quant.onnx"
                ],
            },
        ],
    },
    {
        "id": "paraformer-large-fp32",
        "name_key": "asr.model.paraformer_large_fp32.name",
        "desc_key": "asr.model.paraformer_large_fp32.desc",
        "quantize": False,
        "optional": True,
        "engine": True,
        "size_mb": 884,
        "files": [
            {
                "name": "asr.yaml",
                "size": 1215,
                "urls": [
                    "https://huggingface.co/manyeyes/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx/resolve/main/asr.yaml"
                ],
            },
            {
                "name": "am.mvn",
                "size": 11203,
                "urls": [
                    "https://huggingface.co/manyeyes/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx/resolve/main/am.mvn"
                ],
            },
            {
                "name": "tokens.txt",
                "size": 93676,
                "urls": [
                    "https://huggingface.co/manyeyes/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx/resolve/main/tokens.txt"
                ],
            },
            {
                "name": "model.onnx",
                "size": 884895795,
                "urls": [
                    "https://huggingface.co/manyeyes/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx/resolve/main/model.onnx"
                ],
            },
        ],
    },
    {
        "id": "fsmn-vad",
        "name_key": "asr.model.fsmn_vad.name",
        "desc_key": "asr.model.fsmn_vad.desc",
        "quantize": True,
        "optional": True,
        "engine": False,
        "size_mb": 1,
        "files": [
            {
                "name": "model_quant.onnx",
                "size": 506744,
                "urls": [
                    "https://huggingface.co/funasr/fsmn-vad-onnx/resolve/main/model_quant.onnx"
                ],
            },
            {
                "name": "vad.mvn",
                "size": 8040,
                "urls": ["https://huggingface.co/funasr/fsmn-vad-onnx/resolve/main/vad.mvn"],
            },
            {
                "name": "vad.yaml",
                "size": 1215,
                "urls": ["https://huggingface.co/funasr/fsmn-vad-onnx/resolve/main/vad.yaml"],
            },
        ],
    },
]


class FunasrEngineError(RuntimeError):
    """本地推理失败；``message`` 为可直接展示给用户的人话。"""


def models_dir() -> Path:
    """FunASR 模型根目录（``tools/funasr/``）。"""
    return tools_dir() / "funasr"


def model_dir(model_id: str) -> Path:
    """单个模型的目录（``tools/funasr/<model-id>/``）。"""
    return models_dir() / model_id


def find_spec(model_id: str) -> dict | None:
    """按 id 查模型清单；未知 id 返回 None。"""
    for spec in MODEL_CATALOG:
        if spec["id"] == model_id:
            return spec
    return None


def is_model_ready(model_id: str, quantize: bool | None = None) -> bool:
    """模型是否已下载完整（含对应 ONNX + 配置 + CMVN）。

    Args:
        model_id: 模型 id。
        quantize: 检查 int8 还是 fp32 的 ONNX；None 时用清单默认。
    """
    spec = find_spec(model_id)
    if spec is None:
        return False
    if quantize is None:
        quantize = bool(spec["quantize"])
    d = model_dir(model_id)
    onnx = d / ("model_quant.onnx" if quantize else "model.onnx")
    if not onnx.is_file():
        return False
    config_ok = any((d / name).is_file() for name in ("config.yaml", "asr.yaml", "vad.yaml"))
    cmvn_ok = any((d / name).is_file() for name in ("am.mvn", "vad.mvn"))
    return config_ok and cmvn_ok


def find_ready_model() -> str | None:
    """返回第一个可用于转写的已就绪模型 id（按清单顺序）；没有则 None。"""
    for spec in MODEL_CATALOG:
        if not spec.get("engine", False):
            continue
        if is_model_ready(spec["id"], spec["quantize"]):
            return spec["id"]
    return None


# ---------------------------------------------------------------------------
# 推理（进程内单例，懒加载）
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_instances: dict[str, object] = {}


def _get_model(model_id: str, quantize: bool, threads: int) -> object:
    """懒加载 Paraformer 推理器；同 model+quantize 只构造一次。

    线程约定：onnxruntime 的 ``InferenceSession.run`` 是线程安全的，这里只在
    创建时加锁，推理本身不持锁，避免串行化多段转写。
    """
    key = f"{model_id}:{1 if quantize else 0}"
    with _lock:
        inst = _instances.get(key)
        if inst is None:
            from .funasr.paraformer_bin import Paraformer  # 延迟导入，应用启动不拉 onnxruntime

            inst = Paraformer(
                str(model_dir(model_id)),
                batch_size=1,
                quantize=quantize,
                intra_op_num_threads=int(threads),
            )
            _instances[key] = inst
        return inst


def reset_cache() -> None:
    """清空推理器缓存（测试用）。"""
    with _lock:
        _instances.clear()


def transcribe_local(
    wav_path: str,
    model_id: str = DEFAULT_MODEL_ID,
    quantize: bool | None = None,
    threads: int = DEFAULT_THREADS,
) -> str:
    """用本地模型把一段 16k wav 转成文字。

    Args:
        wav_path: 16k 单声道 wav 路径（asr_worker 已保证规格）。
        model_id: 模型清单里的 id。
        quantize: 是否用 int8 模型；None 时用清单默认。
        threads: onnxruntime CPU intra-op 线程数。

    Returns:
        识别出的纯文本（已清理 CJK 间空格）。

    Raises:
        FunasrEngineError: 模型未下载 / 音频无法读取 / 推理失败，message 可直接展示。
    """
    spec = find_spec(model_id)
    if spec is None:
        raise FunasrEngineError(f"未知模型：{model_id}")
    if quantize is None:
        quantize = bool(spec["quantize"])

    if not is_model_ready(model_id, quantize):
        raise FunasrEngineError(
            f"本地模型 {spec['name_key']} 未下载（tools/funasr/{model_id}/）；"
            f"请先在「模型管理」中下载，或启用服务模式"
        )

    try:
        model = _get_model(model_id, quantize, threads)
        results = model(wav_path)
    except FunasrEngineError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成人话
        raise FunasrEngineError(f"本地推理失败：{exc}") from exc

    text = ""
    if results:
        first = results[0]
        text = first.get("preds", first) if isinstance(first, dict) else first
        if isinstance(text, (list, tuple)):
            text = text[0] if text else ""
    return clean_cjk_spaces((text or "").strip())
