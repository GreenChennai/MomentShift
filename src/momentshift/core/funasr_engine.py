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
from .hardware import cached_asr_device
from .logger import get_logger
from .platform import tools_dir

log = get_logger("funasr_engine")

DEFAULT_MODEL_ID = "paraformer-large"
DEFAULT_THREADS = 8

# 模型清单（单一事实来源，UI / 下载 / 检测共用）。
# - ``kind``：``"asr"``（可被转写流程选用）/ ``"vad"``（语音分段）/ ``"spk"``（说话人）。
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


def _spec_primary_onnx(spec: dict, quantize: bool) -> str:
    """返回该模型就绪检测用的主 ONNX 文件名。"""
    if spec.get("kind") == "spk":
        # CAM++ 是单文件模型，文件名固定
        for f in spec["files"]:
            if f["name"].endswith(".onnx"):
                return f["name"]
    return "model_quant.onnx" if quantize else "model.onnx"


def spec_is_ready(spec: dict, quantize: bool | None = None) -> bool:
    """按清单条目检查模型是否已下载完整（含 ONNX + 必需附属文件）。"""
    if quantize is None:
        quantize = bool(spec.get("quantize", False))
    d = model_dir(spec["id"])
    onnx = d / _spec_primary_onnx(spec, quantize)
    if not onnx.is_file():
        return False
    if spec.get("kind") == "spk":
        # CAM++ 单文件模型：onnx 就绪即就绪
        return True
    config_ok = any((d / name).is_file() for name in ("config.yaml", "asr.yaml", "vad.yaml"))
    cmvn_ok = any((d / name).is_file() for name in ("am.mvn", "vad.mvn"))
    vocab_ok = True
    if spec.get("kind") == "asr" and spec["id"] == "sensevoice-small":
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
    """返回第一个可用于转写的已就绪模型 id（按清单顺序）；没有则 None。"""
    for spec in MODEL_CATALOG:
        if not spec.get("engine", False):
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
