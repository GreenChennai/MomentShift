"""FunASR 本地推理引擎：模型管理 + 转写封装。

职责边界：
- 做：定位 ``tools/funasr/`` 下的模型、检测模型是否就绪、懒加载推理器
  （进程内单例）、把 16k wav 转成文本；v0.8.5 起支持三种推理器：
  - Paraformer（字级中文，CPU int8 ≈13x 实时）
  - SenseVoiceSmall（多语种 + 标点 + 情绪/事件标签，结构化输出首选）
  - FSMN-VAD（语音活动检测，给时间戳/分段）
  - CAM++（说话人嵌入，给说话人标签）
- 不做：不下载模型（``core/funasr_download``）；不弹界面（``gui/asr_interface``）。

模型不随软件分发：下载目标目录 ``tools/funasr/<model-id>/``（``tools/`` 已在
.gitignore，模型绝不进 repo / 不打包）。引擎与用户 FunASR 部署共用同一套
ONNX 模型。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .asr_client import clean_cjk_spaces
from .funasr.utils.postprocess_utils import rich_transcription_postprocess
from .funasr.utils.wav_io import load_wav
from .hardware import cached_asr_device, nvidia_cuda_available
from .logger import get_logger
from .platform import tools_dir

log = get_logger("funasr_engine")

DEFAULT_MODEL_ID = "paraformer-large"
DEFAULT_THREADS = 8

# 模型清单（单一事实来源，UI / 下载 / 检测共用）。
# - ``kind``：``"asr"``（可被转写流程选用）/ ``"vad"``（语音分段）/ ``"spk"``（说话人）
#   / ``"punc"``（标点恢复）/ ``"emo"``（情感识别）。
# - ``category``（v0.8.13）：``"main"`` = 主要模型（负责转写本身的 ASR 模型），
#   ``"optional"`` = 可选功能模型（分段 / 说话人 / 标点 / 情感等增强能力）。
#   UI「模型管理」按此分两组展示并排序，见 ``gui.asr_interface``。
# - ``files`` 里的 ``urls`` 按优先级排列，下载时逐个尝试。
# - ``hw_req``（可选）：硬件要求，不满足时 UI 禁用下载按钮，见
#   ``core.hardware.model_hw_satisfied``。
MODEL_CATALOG: list[dict] = [
    {
        "id": "paraformer-large",
        "name_key": "asr.model.paraformer_large.name",
        "desc_key": "asr.model.paraformer_large.desc",
        "quantize": True,
        "optional": False,
        "engine": True,
        "kind": "asr",
        "category": "main",
        "size_mb": 238,
        "files": [
            {
                "name": "config.yaml",
                "size": 56666,
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
        "kind": "asr",
        "category": "main",
        "size_mb": 884,
        # 884MB 模型 + 运行时开销：内存不足 4GB 的机器下载意义不大（v0.8.5 门控）
        "hw_req": {"min_ram_gb": 4},
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
        # v0.8.5：SenseVoiceSmall（ONNX int8，多语种 + 标点 + 情绪/事件）。
        # 镜像自 ModelScope iic/SenseVoiceSmall-onnx；tokens.json 即
        # sentencepiece 词表（纯 Python 解码，不装 sentencepiece）。
        "id": "sensevoice-small",
        "name_key": "asr.model.sensevoice.name",
        "desc_key": "asr.model.sensevoice.desc",
        "quantize": True,
        "optional": True,
        "engine": True,
        "kind": "asr",
        "category": "main",
        "size_mb": 241,
        "files": [
            {
                "name": "model_quant.onnx",
                "size": 241 * 1024 * 1024,
                "urls": [
                    "https://huggingface.co/haixuantao/SenseVoiceSmall-onnx/resolve/main/model_quant.onnx"
                ],
            },
            {
                "name": "config.yaml",
                "size": 1855,
                "urls": [
                    "https://huggingface.co/haixuantao/SenseVoiceSmall-onnx/resolve/main/config.yaml"
                ],
            },
            {
                "name": "am.mvn",
                "size": 11200,
                "urls": [
                    "https://huggingface.co/haixuantao/SenseVoiceSmall-onnx/resolve/main/am.mvn"
                ],
            },
            {
                "name": "tokens.json",
                "size": 352064,
                "urls": [
                    "https://huggingface.co/haixuantao/SenseVoiceSmall-onnx/resolve/main/tokens.json"
                ],
            },
        ],
    },
    {
        # v0.8.5：FSMN-VAD 由「仅下载预留」升级为结构化输出实际使用。
        "id": "fsmn-vad",
        "name_key": "asr.model.fsmn_vad.name",
        "desc_key": "asr.model.fsmn_vad.desc",
        "quantize": True,
        "optional": True,
        "engine": False,
        "kind": "vad",
        "category": "optional",
        "size_mb": 1,
        "files": [
            {
                "name": "model_quant.onnx",
                "size": 508876,
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
    {
        # v0.8.5：CAM++ 说话人嵌入（ONNX 单文件，CPU 友好）。HF 镜像：
        # welcomyou/campplus-3dspeaker-200k-onnx（28MB，192 维嵌入）。
        "id": "cam++",
        "name_key": "asr.model.campplus.name",
        "desc_key": "asr.model.campplus.desc",
        "quantize": False,
        "optional": True,
        "engine": False,
        "kind": "spk",
        "category": "optional",
        "size_mb": 28,
        "files": [
            {
                "name": "campplus_cn_en_common_200k.onnx",
                "size": 28283928,
                "urls": [
                    "https://huggingface.co/welcomyou/campplus-3dspeaker-200k-onnx/resolve/main/campplus_cn_en_common_200k.onnx"
                ],
            },
        ],
    },
    # =====================================================================
    # v0.8.9：按 FunASR 官方 Model Zoo 扩充模型清单（只列出、不内置推理）。
    # engine=False = 本地推理暂不支持（下载按钮灰显 + 提示），仍可「前往下载」；
    # 带 hw_req 的按硬件门控（无 NVIDIA CUDA 时下载按钮灰显，CPU 可用的正常）。
    # =====================================================================
    {
        "id": "fun-asr-nano",
        "name_key": "asr.model.fun_asr_nano.name",
        "desc_key": "asr.model.fun_asr_nano.desc",
        "quantize": False,
        "optional": True,
        "engine": False,
        "kind": "asr",
        "category": "main",
        "size_mb": 800,
        "hw_req": {"nvidia_cuda": True, "min_ram_gb": 8},
        "page_url": "https://huggingface.co/FunAudioLLM/Fun-ASR-Nano-2512",
        "files": [],
    },
    {
        "id": "fun-asr-mlt-nano",
        "name_key": "asr.model.fun_asr_mlt_nano.name",
        "desc_key": "asr.model.fun_asr_mlt_nano.desc",
        "quantize": False,
        "optional": True,
        "engine": False,
        "kind": "asr",
        "category": "main",
        "size_mb": 800,
        "hw_req": {"nvidia_cuda": True, "min_ram_gb": 8},
        "page_url": "https://huggingface.co/FunAudioLLM/Fun-ASR-MLT-Nano-2512",
        "files": [],
    },
    {
        "id": "qwen3-asr",
        "name_key": "asr.model.qwen3_asr.name",
        "desc_key": "asr.model.qwen3_asr.desc",
        "quantize": False,
        "optional": True,
        "engine": False,
        "kind": "asr",
        "category": "main",
        "size_mb": 1700,
        "hw_req": {"nvidia_cuda": True, "min_ram_gb": 16},
        "page_url": "https://github.com/modelscope/FunASR/blob/main/examples/industrial_data_pretraining/qwen3_asr",
        "files": [],
    },
    {
        "id": "glm-asr-nano",
        "name_key": "asr.model.glm_asr_nano.name",
        "desc_key": "asr.model.glm_asr_nano.desc",
        "quantize": False,
        "optional": True,
        "engine": False,
        "kind": "asr",
        "category": "main",
        "size_mb": 1500,
        "hw_req": {"nvidia_cuda": True, "min_ram_gb": 16},
        "page_url": "https://github.com/modelscope/FunASR/blob/main/examples/industrial_data_pretraining/glm_asr",
        "files": [],
    },
    {
        "id": "whisper-large-v3",
        "name_key": "asr.model.whisper_large_v3.name",
        "desc_key": "asr.model.whisper_large_v3.desc",
        "quantize": False,
        "optional": True,
        "engine": False,
        "kind": "asr",
        "category": "main",
        "size_mb": 1550,
        "hw_req": {"nvidia_cuda": True, "min_ram_gb": 8},
        "page_url": "https://github.com/modelscope/FunASR/blob/main/examples/industrial_data_pretraining/whisper",
        "files": [],
    },
    {
        "id": "whisper-large-v3-turbo",
        "name_key": "asr.model.whisper_turbo.name",
        "desc_key": "asr.model.whisper_turbo.desc",
        "quantize": False,
        "optional": True,
        "engine": False,
        "kind": "asr",
        "category": "main",
        "size_mb": 809,
        "hw_req": {"nvidia_cuda": True, "min_ram_gb": 8},
        "page_url": "https://github.com/modelscope/FunASR/blob/main/examples/industrial_data_pretraining/whisper",
        "files": [],
    },
    {
        # v0.8.11：ct-punc CPU 可用（移植 punc_bin 精简版 + 标点恢复引擎）
        #
        # v0.8.13 修复下载 404：原先指向 HF ``funasr/ct-punc``，但该仓库**只有
        # PyTorch 权重**（model.pt 1.13GB），根本不存在 model_quant.onnx /
        # model.onnx —— 既下载 404，``CT_Transformer``（纯 ONNX 推理）也永远
        # 加载不了。改用已验证可下载的 ONNX 导出镜像
        # ``modelscope.cn/models/botaruibo/punc_ct-onnx``（master 分支）：
        #   model_quant.onnx 282,752,912 / tokens.json 4,207,480 / config.yaml 810
        # 该仓库只有量化版（无 fp32 model.onnx），故不再列 fp32 条目。
        "id": "ct-punc",
        "name_key": "asr.model.ct_punc.name",
        "desc_key": "asr.model.ct_punc.desc",
        "quantize": True,
        "optional": True,
        "engine": True,
        "kind": "punc",
        "category": "optional",
        "size_mb": 274,
        "page_url": "https://modelscope.cn/models/botaruibo/punc_ct-onnx",
        "files": [
            {
                "name": "model_quant.onnx",
                "size": 282752912,
                "urls": [
                    "https://modelscope.cn/models/botaruibo/punc_ct-onnx/resolve/master/model_quant.onnx"
                ],
            },
            {
                "name": "config.yaml",
                "size": 810,
                "urls": [
                    "https://modelscope.cn/models/botaruibo/punc_ct-onnx/resolve/master/config.yaml"
                ],
            },
            {
                "name": "tokens.json",
                "size": 4207480,
                "urls": [
                    "https://modelscope.cn/models/botaruibo/punc_ct-onnx/resolve/master/tokens.json"
                ],
            },
        ],
    },
    {
        # v0.8.13：emotion2vec+large（语音情感识别）。
        #
        # 官方只发布 PyTorch 权重（model.pt 1.86GB），推理走 ``funasr.AutoModel``
        # → 需要 torch + 完整 funasr 包，且实测只在 NVIDIA CUDA 上可用；本软件
        # 内置的是纯 ONNX 精简推理栈，**不捆绑 torch**。
        # 处理方式（用户要求「留有相关调用设置」）：
        #   - 保留完整下载配置（engine=True → 满足硬件时可正常一键下载）；
        #   - hw_req 门控 NVIDIA CUDA，非 N 卡平台下载按钮灰显并说明原因；
        #   - 调用入口见 ``transcribe_emotion()``，运行时按 FunASR 官方用法
        #     AutoModel(model=...).generate(granularity="utterance") 调用，
        #     缺依赖时抛出可读错误而不是崩溃。
        "id": "emotion2vec-large",
        "name_key": "asr.model.emotion2vec.name",
        "desc_key": "asr.model.emotion2vec.desc",
        "quantize": False,
        "optional": True,
        "engine": True,
        "kind": "emo",
        "category": "optional",
        "size_mb": 1856,
        "hw_req": {"nvidia_cuda": True, "min_ram_gb": 8},
        "page_url": "https://huggingface.co/emotion2vec/emotion2vec_plus_large",
        "files": [
            {
                "name": "model.pt",
                "size": 1945790254,
                "urls": [
                    "https://huggingface.co/emotion2vec/emotion2vec_plus_large/resolve/main/model.pt"
                ],
            },
            {
                "name": "config.yaml",
                "size": 5552,
                "urls": [
                    "https://huggingface.co/emotion2vec/emotion2vec_plus_large/resolve/main/config.yaml"
                ],
            },
            {
                "name": "configuration.json",
                "size": 343,
                "urls": [
                    "https://huggingface.co/emotion2vec/emotion2vec_plus_large/resolve/main/configuration.json"
                ],
            },
            {
                "name": "tokens.txt",
                "size": 119,
                "urls": [
                    "https://huggingface.co/emotion2vec/emotion2vec_plus_large/resolve/main/tokens.txt"
                ],
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


def ensure_model_dirs() -> int:
    """为模型清单里每个模型预建二级文件夹 ``tools/funasr/<id>/``（幂等）。

    v0.8.8 Bug3：模型从一开始就按分类放二级文件夹，用户手动下载时按
    「打开文件夹」进入对应目录存放即可，不再依赖文件名自动归类。
    """
    n = 0
    for spec in MODEL_CATALOG:
        d = model_dir(spec["id"])
        try:
            d.mkdir(parents=True, exist_ok=True)
            n += 1
        except OSError as exc:
            log.warning("创建模型目录 %s 失败：%s", d, exc)
    return n


def find_spec(model_id: str) -> dict | None:
    """按 id 查模型清单；未知 id 返回 None。"""
    for spec in MODEL_CATALOG:
        if spec["id"] == model_id:
            return spec
    return None


def _spec_primary_onnx(spec: dict, quantize: bool) -> str:
    """返回该模型就绪检测用的主权重文件名。

    v0.8.13：``emo``（emotion2vec）是 PyTorch 权重 ``model.pt``，不是 ONNX。
    """
    kind = spec.get("kind")
    if kind == "emo":
        return "model.pt"
    if kind == "spk":
        # CAM++ 是单文件模型，文件名固定
        for f in spec["files"]:
            if f["name"].endswith(".onnx"):
                return f["name"]
    return "model_quant.onnx" if quantize else "model.onnx"


def spec_is_ready(spec: dict, quantize: bool | None = None) -> bool:
    """按清单条目检查模型是否已下载完整（主权重 + 必需附属文件）。

    v0.8.13 修复：原实现对所有非 spk 模型一律要求 CMVN（``am.mvn``/``vad.mvn``），
    但 ``punc``（ct-punc）与 ``emo``（emotion2vec）根本没有 CMVN 文件 ——
    导致这两类模型即使下载完整也永远判定为「未下载」。现按 kind 分别判定。
    """
    if quantize is None:
        quantize = bool(spec.get("quantize", False))
    d = model_dir(spec["id"])
    primary = d / _spec_primary_onnx(spec, quantize)
    if not primary.is_file():
        return False
    kind = spec.get("kind", "asr")
    if kind == "spk":
        # CAM++ 单文件模型：onnx 就绪即就绪
        return True
    if kind == "punc":
        # CT-Transformer：ONNX + config.yaml + tokens.json（无 CMVN）
        return (d / "config.yaml").is_file() and (d / "tokens.json").is_file()
    if kind == "emo":
        # emotion2vec：model.pt + config.yaml（无 CMVN、无 ONNX）
        return (d / "config.yaml").is_file()
    config_ok = any((d / name).is_file() for name in ("config.yaml", "asr.yaml", "vad.yaml"))
    cmvn_ok = any((d / name).is_file() for name in ("am.mvn", "vad.mvn"))
    vocab_ok = True
    if kind == "asr" and spec["id"] == "sensevoice-small":
        # SenseVoice 解码需要 tokens.json（sentencepiece 词表）
        vocab_ok = (d / "tokens.json").is_file()
    return config_ok and cmvn_ok and vocab_ok


def is_model_ready(model_id: str, quantize: bool | None = None) -> bool:
    """模型是否已下载完整（含对应 ONNX + 配置 + CMVN）。

    Args:
        model_id: 模型 id。
        quantize: 检查 int8 还是 fp32 的 ONNX；None 时用清单默认。
    """
    spec = find_spec(model_id)
    if spec is None:
        return False
    return spec_is_ready(spec, quantize)


# v0.8.7 Bug6：手动放置模型不被识别的自动归位。
# 用户可能把 model_quant.onnx / config.yaml 等直接放进 ``tools/funasr/`` 根目录
# （没建 ``tools/funasr/<id>/`` 二级目录），软件默认按 <id>/ 检测不到。
# 这里扫描根目录裸文件，按「文件名归属」自动移入对应模型子目录，幂等。
_LOOSE_ASR_FILES = {"model_quant.onnx", "model.onnx", "config.yaml", "asr.yaml", "am.mvn", "tokens.json"}
_LOOSE_VAD_FILES = {"model_quant.onnx", "vad.mvn", "vad.yaml"}


def relocate_loose_model_files() -> int:
    """把 ``tools/funasr/`` 根目录的裸模型文件自动归位到对应 ``<id>/`` 子目录。

    Returns:
        成功归位的文件数（幂等：已就位/已存在的跳过）。
    """
    root = models_dir()
    if not root.is_dir():
        return 0
    loose = [p for p in root.iterdir() if p.is_file()]
    if not loose:
        return 0
    names = {p.name for p in loose}
    moved = 0

    def _move_group(target_id: str, files: list) -> None:
        nonlocal moved
        target = model_dir(target_id)
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            dst = target / f.name
            if dst.exists():
                continue  # 目标已存在不覆盖
            try:
                os.replace(str(f), str(dst))
                moved += 1
            except OSError as exc:
                log.warning("模型文件归位失败：%s（%s）", f, exc)

    # 归属判定（根目录文件组来自同一模型时按特征归位）：
    # - vad.mvn / vad.yaml → fsmn-vad
    # - campplus*.onnx → cam++
    # - 其余 ASR 文件 → 有 tokens.json 归 sensevoice-small，否则 paraformer-large
    if "vad.mvn" in names or "vad.yaml" in names:
        _move_group("fsmn-vad", [p for p in loose if p.name in _LOOSE_VAD_FILES])
    campplus = [p for p in loose if p.name.startswith("campplus") and p.name.endswith(".onnx")]
    if campplus:
        _move_group("cam++", campplus)
    asr_files = [p for p in loose if p.name in _LOOSE_ASR_FILES]
    if asr_files:
        target_id = "sensevoice-small" if "tokens.json" in names else "paraformer-large"
        _move_group(target_id, asr_files)
    return moved


def find_ready_model() -> str | None:
    """返回第一个可用于转写的已就绪**ASR**模型 id（按清单顺序）；没有则 None。

    v0.8.13 修复：ct-punc（punc）与 emotion2vec（emo）现在也是 ``engine=True``，
    若不限定 ``kind == "asr"``，自动选模会把标点/情感模型当成转写模型返回。
    """
    for spec in MODEL_CATALOG:
        if spec.get("kind", "asr") != "asr" or not spec.get("engine", False):
            continue
        if is_model_ready(spec["id"], spec["quantize"]):
            return spec["id"]
    return None


def find_ready_vad_model() -> str | None:
    """返回已就绪的 VAD 模型 id（结构化输出分段用）；没有则 None。"""
    for spec in MODEL_CATALOG:
        if spec.get("kind") != "vad":
            continue
        if is_model_ready(spec["id"], spec["quantize"]):
            return spec["id"]
    return None


def find_ready_spk_model() -> str | None:
    """返回已就绪的说话人模型 id（结构化输出说话人标签用）；没有则 None。"""
    for spec in MODEL_CATALOG:
        if spec.get("kind") != "spk":
            continue
        if is_model_ready(spec["id"], spec["quantize"]):
            return spec["id"]
    return None


def resolve_model_id(model_id: str = "") -> str | None:
    """把配置值解析成实际模型 id：空串 = 自动（第一个已就绪模型）。"""
    if model_id:
        return model_id if is_model_ready(model_id) else None
    return find_ready_model()


def resolve_provider(device: str = "auto") -> str | None:
    """把 ``asrDevice`` 配置值（auto/cpu/cuda）解析成推理 provider。

    Returns:
        ``"cpu"`` / ``"cuda"``；``"cuda"`` 仅当硬件检测通过（N 卡 + CUDA EP）。
    """
    if device == "cpu":
        return "cpu"
    if device == "cuda":
        return "cuda"
    return "cuda" if cached_asr_device() == "cuda" else "cpu"


# ---------------------------------------------------------------------------
# 推理（进程内单例，懒加载）
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_instances: dict[str, object] = {}


def _get_model(
    model_id: str,
    quantize: bool,
    threads: int,
    provider: str | None = None,
) -> object:
    """懒加载推理器（Paraformer / SenseVoice / FSMN-VAD / CAM++）。

    线程约定：onnxruntime 的 ``InferenceSession.run`` 是线程安全的，这里只在
    创建时加锁，推理本身不持锁，避免串行化多段转写。
    """
    spec = find_spec(model_id)
    if spec is None:
        raise FunasrEngineError(f"未知模型：{model_id}")
    key = f"{model_id}:{1 if quantize else 0}:{provider or 'auto'}"
    with _lock:
        inst = _instances.get(key)
        if inst is None:
            kind = spec.get("kind", "asr")
            if kind == "spk":
                from .funasr.spk_bin import Campplus

                inst = Campplus(
                    str(model_dir(model_id)),
                    intra_op_num_threads=int(threads),
                    provider=provider,
                )
            elif kind == "vad":
                from .funasr.vad_bin import Fsmn_vad

                inst = Fsmn_vad(
                    str(model_dir(model_id)),
                    batch_size=1,
                    quantize=quantize,
                    intra_op_num_threads=int(threads),
                    provider=provider,
                )
            elif model_id == "sensevoice-small":
                from .funasr.sensevoice_bin import SenseVoiceSmall

                inst = SenseVoiceSmall(
                    str(model_dir(model_id)),
                    batch_size=1,
                    quantize=quantize,
                    intra_op_num_threads=int(threads),
                    provider=provider,
                )
            else:
                from .funasr.paraformer_bin import Paraformer

                inst = Paraformer(
                    str(model_dir(model_id)),
                    batch_size=1,
                    quantize=quantize,
                    intra_op_num_threads=int(threads),
                    provider=provider,
                )
            _instances[key] = inst
        return inst


def reset_cache() -> None:
    """清空推理器缓存（测试用）。"""
    with _lock:
        _instances.clear()


def _postprocess_text(text: str, model_id: str) -> str:
    """按模型类型清理输出文本。

    - SenseVoice：``rich_transcription_postprocess`` 把 ``<|zh|>`` 等标签换成
      emoji / 去掉，并保留标点；
    - Paraformer：直接清理 CJK 间空格。
    """
    text = (text or "").strip()
    if model_id == "sensevoice-small":
        text = rich_transcription_postprocess(text)
    return clean_cjk_spaces(text)


# ---------------------------------------------------------------------------
# 标点恢复（v0.8.11，可选：CT-Transformer ct-punc，CPU 可用）
# ---------------------------------------------------------------------------
_punc_cache: dict = {}  # {model_id: CT_Transformer 实例}（类型注解避开 ruff F821）
_punc_lock = threading.Lock()


def _get_punc_model(model_id: str = "ct-punc"):
    """懒加载 + 进程内单例缓存标点模型。"""
    from .funasr.punc_bin import CT_Transformer

    with _punc_lock:
        cached = _punc_cache.get(model_id)
        if cached is not None:
            return cached
        spec = find_spec(model_id)
        if spec is None or spec.get("kind") != "punc":
            raise FunasrEngineError(f"标点模型不可用：{model_id}")
        if not is_model_ready(model_id, spec.get("quantize")):
            raise FunasrEngineError(f"标点模型 {model_id} 未下载")
        model = CT_Transformer(
            model_dir=str(model_dir(model_id)),
            intra_op_num_threads=DEFAULT_THREADS,
        )
        _punc_cache[model_id] = model
        return model


def transcribe_with_punc(text: str, model_id: str = "ct-punc") -> str:
    """对转写结果加标点。

    Args:
        text: ASR 输出的纯文本（已 ``clean_cjk_spaces``）。
        model_id: 标点模型 id（默认 ``ct-punc``，CPU 可用）。

    Returns:
        加标点后的文本；失败时返回原文（不抛异常给 worker）。
    """
    text = (text or "").strip()
    if not text:
        return text
    try:
        model = _get_punc_model(model_id)
        result, _punc_ids = model(text)
        return result or text
    except FunasrEngineError as exc:
        log.warning("标点恢复跳过：%s", exc)
        return text
    except Exception as exc:  # noqa: BLE001 - 标点失败不影响转写结果
        log.warning("标点恢复失败（保留原文）：%s", exc)
        return text


# ---------------------------------------------------------------------------
# 语音情感识别（v0.8.13，可选：emotion2vec+large，需 NVIDIA CUDA + 完整 funasr）
# ---------------------------------------------------------------------------
_emo_cache: dict = {}  # {model_id: funasr.AutoModel 实例}
_emo_lock = threading.Lock()

# emotion2vec_plus_large 的标签表（官方 tokens.txt 顺序，中文/英文双语标签）
EMOTION_LABELS = (
    "生气/angry",
    "厌恶/disgusted",
    "恐惧/fearful",
    "开心/happy",
    "中立/neutral",
    "其他/other",
    "难过/sad",
    "吃惊/surprised",
    "<unk>",
)


def emotion_available(model_id: str = "emotion2vec-large") -> tuple[bool, str]:
    """检查情感识别是否可用（不加载模型，供 UI 灰显/提示用）。

    Returns:
        ``(可用, 原因键)``；原因键为 ``""`` / ``"no_model"``（未下载）/
        ``"nvidia_cuda"``（非 N 卡平台）/ ``"no_funasr"``（缺完整 funasr+torch）。
    """
    spec = find_spec(model_id)
    if spec is None or spec.get("kind") != "emo":
        return False, "no_model"
    if not is_model_ready(model_id, spec.get("quantize")):
        return False, "no_model"
    # v0.8.18 Bug1：按「硬件是否具备 NVIDIA CUDA」判定，而非 onnxruntime 的
    # CUDA EP（随包是 CPU 版 onnxruntime，但 emotion2vec 走外部 funasr+torch，
    # 用户自有部署可用其 CUDA；5070 Ti 等真实 N 卡不应被误报为不支持）。
    if not nvidia_cuda_available():
        return False, "nvidia_cuda"
    try:
        import importlib.util

        if importlib.util.find_spec("funasr") is None:
            return False, "no_funasr"
    except (ImportError, ValueError):
        return False, "no_funasr"
    return True, ""


def _get_emotion_model(model_id: str = "emotion2vec-large"):
    """懒加载 + 进程内单例缓存情感识别模型（``funasr.AutoModel``）。

    与其余推理器不同：emotion2vec 官方只发布 PyTorch 权重，必须依赖**外部**
    完整 funasr 包（含 torch）。本软件内置的是纯 ONNX 精简栈，不捆绑 torch，
    因此这里显式检查依赖并抛出可读错误，绝不让 ImportError 冒到 UI。
    """
    ok, reason = emotion_available(model_id)
    if not ok:
        raise FunasrEngineError(
            {
                "no_model": f"情感识别模型 {model_id} 未下载",
                "nvidia_cuda": "情感识别需要 NVIDIA CUDA 环境，当前平台不支持",
                "no_funasr": "情感识别需要完整 funasr 包（含 PyTorch），请先安装：pip install funasr torch",
            }.get(reason, f"情感识别不可用：{reason}")
        )
    with _emo_lock:
        cached = _emo_cache.get(model_id)
        if cached is not None:
            return cached
        try:
            from funasr import AutoModel  # 外部完整 funasr（非内置精简栈）
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise FunasrEngineError(
                "情感识别需要完整 funasr 包（含 PyTorch），请先安装：pip install funasr torch"
            ) from exc
        try:
            model = AutoModel(model=str(model_dir(model_id)), disable_update=True)
        except Exception as exc:  # noqa: BLE001 - 第三方库异常类型不可控
            raise FunasrEngineError(f"情感识别模型加载失败：{exc}") from exc
        _emo_cache[model_id] = model
        return model


def transcribe_emotion(
    wav_path: str,
    model_id: str = "emotion2vec-large",
    top_k: int = 3,
) -> list[tuple[str, float]]:
    """识别一段 16k wav 的说话人情感（emotion2vec+large）。

    按 FunASR 官方用法调用::

        AutoModel(model=...).generate(wav, granularity="utterance",
                                      extract_embedding=False)

    Args:
        wav_path: 16k 单声道 wav 路径。
        model_id: 情感模型 id（默认 ``emotion2vec-large``）。
        top_k: 返回置信度最高的前 k 个标签。

    Returns:
        ``[(标签, 分数), ...]``，按分数降序；识别不出内容时返回空列表。

    Raises:
        FunasrEngineError: 模型未下载 / 平台不支持 / 缺依赖 / 推理失败，
            ``message`` 可直接展示给用户。
    """
    model = _get_emotion_model(model_id)
    try:
        res = model.generate(
            wav_path,
            granularity="utterance",
            extract_embedding=False,
        )
    except Exception as exc:  # noqa: BLE001 - 第三方库异常类型不可控
        raise FunasrEngineError(f"情感识别失败：{exc}") from exc
    if not res:
        return []
    item = res[0] if isinstance(res, list) else res
    labels = list(item.get("labels") or EMOTION_LABELS)
    scores = list(item.get("scores") or [])
    pairs = [(str(lab), float(sc)) for lab, sc in zip(labels, scores)]
    pairs.sort(key=lambda kv: kv[1], reverse=True)
    return pairs[: max(1, int(top_k))]


def transcribe_local(
    wav_path: str,
    model_id: str = DEFAULT_MODEL_ID,
    quantize: bool | None = None,
    threads: int = DEFAULT_THREADS,
    provider: str | None = None,
    use_itn: bool = True,
) -> str:
    """用本地模型把一段 16k wav 转成文字。

    Args:
        wav_path: 16k 单声道 wav 路径（asr_worker 已保证规格）。
        model_id: 模型清单里的 id（asr 类）。
        quantize: 是否用 int8 模型；None 时用清单默认。
        threads: onnxruntime CPU intra-op 线程数。
        provider: ``"cpu"`` / ``"cuda"`` / None（自动）。
        use_itn: SenseVoice 是否开启逆文本归一化（标点/数字规整）。

    Returns:
        识别出的纯文本。

    Raises:
        FunasrEngineError: 模型未下载 / 音频无法读取 / 推理失败，message 可直接展示。
    """
    spec = find_spec(model_id)
    if spec is None:
        raise FunasrEngineError(f"未知模型：{model_id}")
    if spec.get("kind", "asr") != "asr":
        raise FunasrEngineError(f"模型 {spec['id']} 不能用于直接转写（kind={spec.get('kind')}）")
    if quantize is None:
        quantize = bool(spec["quantize"])

    if not is_model_ready(model_id, quantize):
        raise FunasrEngineError(
            f"本地模型 {spec['name_key']} 未下载（tools/funasr/{model_id}/）；"
            f"请先在「模型管理」中下载，或启用服务模式"
        )

    try:
        model = _get_model(model_id, quantize, threads, provider=provider)
        if model_id == "sensevoice-small":
            raw = model(wav_path, textnorm="withitn" if use_itn else "woitn")
            text = raw[0] if raw else ""
        else:
            results = model(wav_path)
            text = ""
            if results:
                first = results[0]
                text = first.get("preds", first) if isinstance(first, dict) else first
                if isinstance(text, (list, tuple)):
                    text = text[0] if text else ""
    except FunasrEngineError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成人话
        raise FunasrEngineError(f"本地推理失败：{exc}") from exc

    return _postprocess_text(text, model_id)


# ---------------------------------------------------------------------------
# 结构化输出（v0.8.5，功能 2）
# ---------------------------------------------------------------------------
def _vad_segments_ms(wav_path: str, vad_model_id: str, provider: str | None) -> list[list[int]]:
    """用 FSMN-VAD 把整段音频切成语音段，返回 ``[[start_ms, end_ms], ...]``。"""
    spec = find_spec(vad_model_id)
    if spec is None or spec.get("kind") != "vad":
        raise FunasrEngineError(f"VAD 模型不可用：{vad_model_id}")
    if not is_model_ready(vad_model_id, spec["quantize"]):
        raise FunasrEngineError(
            f"VAD 模型 {spec['name_key']} 未下载（tools/funasr/{vad_model_id}/）"
        )
    model = _get_model(vad_model_id, bool(spec["quantize"]), DEFAULT_THREADS, provider=provider)
    return [list(map(int, seg)) for seg in model(wav_path)]


def transcribe_structured(
    wav_path: str,
    model_id: str = "sensevoice-small",
    vad_model_id: str = "fsmn-vad",
    spk_model_id: str | None = None,
    quantize: bool | None = None,
    threads: int = DEFAULT_THREADS,
    provider: str | None = None,
    use_itn: bool = True,
) -> list[dict]:
    """结构化转写：VAD 分段 → 逐段识别 → 可选说话人聚类。

    官方 FunASR 语义：``SenseVoiceSmall + FSMN-VAD + CAM++`` 一条流水线输出
    带时间戳 / 说话人 / 标点的结果，形如 ``[0.6s] 说话人0: 欢迎大家…``。

    Args:
        wav_path: 16k 单声道 wav 路径。
        model_id: ASR 模型 id（推荐 sensevoice-small；paraformer 也可用但无标点）。
        vad_model_id: FSMN-VAD 模型 id。
        spk_model_id: CAM++ 模型 id；None 表示「未下载/不启用说话人」。
        quantize: ASR 模型是否用 int8；None 用清单默认。
        threads: CPU 线程数。
        provider: 推理 provider。
        use_itn: SenseVoice 逆文本归一化开关。

    Returns:
        ``[{"start": 0.6, "end": 3.2, "spk": 0, "text": "…"}, ...]``，
        start/end 单位秒；``spk`` 为 -1 表示「无说话人信息」。

    Raises:
        FunasrEngineError: ASR/VAD 模型未下载、音频无法读取或推理失败。
    """
    spec = find_spec(model_id)
    if spec is None or spec.get("kind", "asr") != "asr":
        raise FunasrEngineError(f"ASR 模型不可用：{model_id}")
    if quantize is None:
        quantize = bool(spec["quantize"])
    if not is_model_ready(model_id, quantize):
        raise FunasrEngineError(
            f"本地模型 {spec['name_key']} 未下载（tools/funasr/{model_id}/）"
        )

    # ① VAD 分段（毫秒）
    segments_ms = _vad_segments_ms(wav_path, vad_model_id, provider)
    if not segments_ms:
        return []

    # ② 逐段转写（读一次波形，切段喂模型）
    waveform = load_wav(wav_path)
    sr = 16000
    model = _get_model(model_id, quantize, threads, provider=provider)
    use_sensevoice = model_id == "sensevoice-small"

    # ③ CAM++ 说话人嵌入（可选）
    spk_model = None
    if spk_model_id:
        spk_spec = find_spec(spk_model_id)
        if spk_spec is not None and spk_spec.get("kind") == "spk" and is_model_ready(
            spk_model_id, spk_spec["quantize"]
        ):
            spk_model = _get_model(
                spk_model_id, bool(spk_spec["quantize"]), threads, provider=provider
            )

    results: list[dict] = []
    for start_ms, end_ms in segments_ms:
        start = start_ms / 1000.0
        end = end_ms / 1000.0
        seg = waveform[int(start_ms * sr // 1000) : int(end_ms * sr // 1000)]
        if seg.size < sr // 20:  # <50ms 的碎片段跳过
            continue
        try:
            if use_sensevoice:
                raw = model(seg, textnorm="withitn" if use_itn else "woitn")
                text = raw[0] if raw else ""
            else:
                outs = model(seg)
                text = ""
                if outs:
                    first = outs[0]
                    text = first.get("preds", first) if isinstance(first, dict) else first
                    if isinstance(text, (list, tuple)):
                        text = text[0] if text else ""
        except Exception as exc:  # noqa: BLE001 - 单段失败不中断整条流水线
            raise FunasrEngineError(f"分段 {start_ms}ms 推理失败：{exc}") from exc
        text = _postprocess_text(text, model_id)
        if not text:
            continue
        if spk_model is not None:
            emb = spk_model.embed_segment(seg)
            results.append({"start": start, "end": end, "spk": -1, "text": text, "_emb": emb})
        else:
            results.append({"start": start, "end": end, "spk": -1, "text": text})

    # ④ 说话人聚类（有嵌入时统一归一次，再回填 spk）
    if spk_model is not None and results:
        embs = [r["_emb"] for r in results]
        from .funasr.spk_bin import cluster_speakers

        labels = cluster_speakers(embs)
        for r, label in zip(results, labels):
            r["spk"] = int(label)
            r.pop("_emb", None)
    else:
        for r in results:
            r.pop("_emb", None)

    return results
