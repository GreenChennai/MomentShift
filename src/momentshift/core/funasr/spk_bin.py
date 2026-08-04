"""CAM++ 说话人嵌入（Speaker Embedding）推理与纯 numpy 聚类。

背景（v0.8.5）：结构化输出需要说话人标签。官方 FunASR 的 ``spk_model="cam++"``
依赖 torch 全栈；本模块用 HF 上的 ONNX 镜像 ``welcomyou/campplus-3dspeaker-200k-onnx``
（``campplus_cn_en_common_200k.onnx``，28MB）实现等价能力，运行时只依赖
numpy/onnxruntime（不新增第三方依赖）：

- 输入：16k 单声道音频 → 80 维 log-mel fbank（复用同包 ``frontend.compute_fbank``，
  kaldi 约定 25ms/10ms/hamming）→ 按句减均值（CMVN）→ shape ``(1, T, 80)``
- 输出：192 维说话人嵌入（L2 归一化后做余弦相似度）
- 聚类：纯 numpy 凝聚层次聚类（平均链接 + 余弦距离阈值），把各语音段的嵌入
  归成若干说话人

模型文件：``campplus_cn_en_common_200k.onnx`` 单个文件即完整模型（无 config /
cmvn），因此本模块按「文件存在即就绪」判定，与 paraformer/vad 的
``is_model_ready`` 检查路径不同（由 ``core/funasr_engine`` 适配）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .utils.frontend import compute_fbank
from .utils.utils import OrtInferSession
from .utils.wav_io import load_wav

# 滑动窗参数（对齐 FunASR spk_model 默认：1.5s 窗 / 0.75s 移）
WINDOW_SEC = 1.5
SHIFT_SEC = 0.75
# 平均链接聚类的余弦距离阈值：> 阈值视为不同说话人
CLUSTER_THRESHOLD = 0.45


class Campplus:
    """CAM++ 说话人嵌入模型（ONNX，CPU 友好，28MB）。"""

    def __init__(
        self,
        model_dir: str | Path = None,
        device_id: str | int = "-1",
        intra_op_num_threads: int = 4,
        provider: str | None = None,
        **kwargs,
    ):
        model_dir = Path(model_dir)
        if not model_dir.is_dir():
            raise FileNotFoundError(f"模型目录不存在：{model_dir}")
        model_file = model_dir / "campplus_cn_en_common_200k.onnx"
        if not model_file.is_file():
            raise FileNotFoundError(
                f"模型文件缺失：{model_file.name}（目录 {model_dir}）"
            )
        self.ort_infer = OrtInferSession(
            str(model_file),
            device_id,
            intra_op_num_threads=intra_op_num_threads,
            provider=provider,
        )

    def __call__(self, wav_content: str | np.ndarray) -> np.ndarray:
        """对一段音频返回一个 192 维说话人嵌入（L2 归一化）。

        Args:
            wav_content: wav 路径或 float32 波形（[-1,1]）。
        Returns:
            shape ``(192,)`` 的 float32 嵌入（已 L2 归一化）。
        """
        if isinstance(wav_content, str):
            waveform = load_wav(wav_content)
        else:
            waveform = np.asarray(wav_content, dtype=np.float32)
        # CAM++ 官方部署用 kaldi 默认窗（povey）；80 维 mel，25ms/10ms
        feats = compute_fbank(
            waveform * (1 << 15), fs=16000, n_mels=80, window_type="povey"
        )
        if feats.shape[0] == 0:
            raise ValueError("音频过短，无法提取说话人嵌入")
        # 按句减均值（CAM++ 官方 ONNX 部署的 CMVN 约定）
        feats = feats - feats.mean(axis=0, keepdims=True)
        embs = self.ort_infer([feats[np.newaxis].astype(np.float32)])[0]
        emb = np.asarray(embs[0], dtype=np.float32)
        norm = float(np.linalg.norm(emb))
        if norm < 1e-8:
            return np.zeros(192, dtype=np.float32)
        return emb / norm

    def embed_segment(self, waveform: np.ndarray) -> np.ndarray:
        """对一段（可能很长的）语音取平均嵌入：超过窗长则滑动窗逐窗取嵌入后平均。

        Args:
            waveform: float32 单声道波形（[-1,1]）。
        Returns:
            shape ``(192,)`` 的 float32 平均嵌入（已 L2 归一化）。
        """
        wave = np.asarray(waveform, dtype=np.float32)
        win = int(WINDOW_SEC * 16000)
        shift = int(SHIFT_SEC * 16000)
        if wave.size <= win:
            return self.__call__(wave)
        starts = list(range(0, wave.size - win + 1, shift))
        if not starts or starts[-1] + win < wave.size:
            starts.append(max(0, wave.size - win))
        embs = [self.__call__(wave[s : s + win]) for s in starts]
        embs = np.asarray(embs, dtype=np.float32)
        avg = embs.mean(axis=0)
        norm = float(np.linalg.norm(avg))
        if norm < 1e-8:
            return np.zeros(192, dtype=np.float32)
        return (avg / norm).astype(np.float32)


# ---------------------------------------------------------------------------
# 纯 numpy 聚类（离屏可测）
# ---------------------------------------------------------------------------
def cosine_similarity_matrix(embeddings: np.ndarray) -> np.ndarray:
    """嵌入矩阵 → 余弦相似度矩阵 ``(n, n)``（行已假定或此处归一化）。"""
    embs = np.asarray(embeddings, dtype=np.float32)
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    norms = np.where(norms < 1e-8, 1.0, norms)
    embs = embs / norms
    return embs @ embs.T


def _average_linkage(dist: np.ndarray, set_a: set[int], set_b: set[int]) -> float:
    """平均链接距离：两个簇之间所有点对余弦距离的均值。"""
    if not set_a or not set_b:
        return float("inf")
    rows = sorted(set_a)
    cols = sorted(set_b)
    total = 0.0
    count = 0
    for i in rows:
        for j in cols:
            total += float(dist[i, j])
            count += 1
    return total / count if count else float("inf")


def cluster_speakers(embeddings: np.ndarray, threshold: float = CLUSTER_THRESHOLD) -> list[int]:
    """平均链接凝聚层次聚类（纯 numpy，余弦距离）。

    Args:
        embeddings: shape ``(n, 192)`` 的说话人嵌入（未归一化也可）。
        threshold: 平均链接余弦距离阈值；> 阈值停止合并。

    Returns:
        ``[label_0, label_1, ...]``，label 为 0 起的说话人序号。
    """
    embs = np.asarray(embeddings, dtype=np.float32)
    n = embs.shape[0]
    if n == 0:
        return []
    if n == 1:
        return [0]
    sim = cosine_similarity_matrix(embs)
    dist = 1.0 - sim
    np.fill_diagonal(dist, 0.0)

    labels = list(range(n))
    members: dict[int, set[int]] = {i: {i} for i in range(n)}
    active = set(range(n))

    while True:
        act = sorted(active)
        best_pair: tuple[int, int] | None = None
        best_d = float("inf")
        for a in range(len(act)):
            for b in range(a + 1, len(act)):
                ci, cj = act[a], act[b]
                d = _average_linkage(dist, members[ci], members[cj])
                if d < best_d:
                    best_d = d
                    best_pair = (ci, cj)
        # 最近簇的距离已超过阈值 → 停止合并
        if best_pair is None or best_d > threshold:
            break
        ci, cj = best_pair
        merged = members[ci] | members[cj]
        for item in members[cj]:
            labels[item] = ci
        members[ci] = merged
        active.discard(cj)

    final = sorted(active)
    relabel = {cid: idx for idx, cid in enumerate(final)}
    return [relabel[labels[i]] for i in range(n)]
