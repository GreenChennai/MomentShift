# Copyright FunASR (https://github.com/alibaba-damo-academy/FunASR). All Rights Reserved.
#  MIT License  (https://opensource.org/licenses/MIT)
#
# 裁剪说明（MomentShift v0.8.5 内置版，相对 funasr_onnx 0.4.2 的 sensevoice_bin.py）：
# - 移除 librosa / pydub：音频加载改用同包 ``utils/wav_io``（16k 单声道 wav；
#   非 wav 或异常报人话错误，格式归一化由 core/asr_worker 统一负责）
# - 移除 modelscope / funasr 自动下载与导出（模型必须已存在于本地目录，
#   下载走 MomentShift 的 ``core/funasr_download``）
# - 移除 ``sentencepiece``：解码改走同包 ``utils/sentencepiece_decode``
#   （tokens.json 词表纯 Python 解码，零新增依赖）
# - 移除 ``read_tags`` 的文件形式（只保留字符串/列表输入：language/textnorm）
#
# 保留的公开接口：``SenseVoiceSmall(model_dir, batch_size, device_id, quantize,
# intra_op_num_threads, provider)`` + ``__call__(wav_path, language, textnorm,
# use_itn) -> [str, ...]``（原始文本，含 ``<|...|>`` 特殊 token，调用方再用
# ``rich_transcription_postprocess`` 清洗）。
#
# 输出约定：官方 SenseVoice 语义下 ``language="auto"`` / ``textnorm="woitn"``
# 是默认（woitn = without inverse text normalization，与 funasr_onnx 一致；
# 需要标点/数字规整时用 ``textnorm="withitn"``）。

from __future__ import annotations

from pathlib import Path

import numpy as np

from .utils.frontend import WavFrontend
from .utils.sentencepiece_decode import decode_ids, load_vocab
from .utils.utils import (
    ONNXRuntimeError,
    OrtInferSession,
    get_logger,
    read_yaml,
)
from .utils.wav_io import load_wav

logging = get_logger()

_CONFIG_NAMES = ("config.yaml", "config.yml")
_CMVN_NAMES = ("am.mvn",)


class SenseVoiceSmall:
    """
    Author: Speech Lab of DAMO Academy, Alibaba Group
    SenseVoice: Speech Foundation Model for Multilingual Speech Understanding
    https://arxiv.org/abs/2406.03086
    """

    def __init__(
        self,
        model_dir: str | Path = None,
        batch_size: int = 1,
        device_id: str | int = "-1",
        quantize: bool = False,
        intra_op_num_threads: int = 4,
        provider: str | None = None,
        **kwargs,
    ):
        model_dir = Path(model_dir)
        if not model_dir.is_dir():
            raise FileNotFoundError(f"模型目录不存在：{model_dir}")

        model_file = model_dir / ("model_quant.onnx" if quantize else "model.onnx")
        if not model_file.is_file():
            raise FileNotFoundError(
                f"模型文件缺失：{model_file.name}（quantize={quantize}，目录 {model_dir}）"
            )

        config_path = None
        for name in _CONFIG_NAMES:
            p = model_dir / name
            if p.is_file():
                config_path = p
                break
        if config_path is None:
            raise FileNotFoundError(f"模型缺少配置文件（{' / '.join(_CONFIG_NAMES)}）")

        cmvn_file = None
        for name in _CMVN_NAMES:
            p = model_dir / name
            if p.is_file():
                cmvn_file = p
                break
        if cmvn_file is None:
            raise FileNotFoundError(f"模型缺少 CMVN 文件（{' / '.join(_CMVN_NAMES)}）")

        config = read_yaml(str(config_path))
        if not config:
            raise ValueError(f"配置文件解析为空：{config_path}")

        self.vocab = load_vocab(model_dir)
        frontend_conf = dict(config.get("frontend_conf") or {})
        frontend_conf["cmvn_file"] = str(cmvn_file)
        self.frontend = WavFrontend(**frontend_conf)
        self.ort_infer = OrtInferSession(
            str(model_file),
            device_id,
            intra_op_num_threads=intra_op_num_threads,
            provider=provider,
        )
        self.batch_size = batch_size
        self.blank_id = 0
        self.lid_dict = {
            "auto": 0, "zh": 3, "en": 4, "yue": 7, "ja": 11, "ko": 12, "nospeech": 13,
        }
        self.lid_int_dict = {
            24884: 3, 24885: 4, 24888: 7, 24892: 11, 24896: 12, 24992: 13,
        }
        self.textnorm_dict = {"withitn": 14, "woitn": 15}
        self.textnorm_int_dict = {25016: 14, 25017: 15}

    def _get_lid(self, lid: str) -> int:
        if lid in self.lid_dict:
            return self.lid_dict[lid]
        raise ValueError(f"The language {lid} is not in {list(self.lid_dict.keys())}")

    def _get_tnid(self, tnid: str) -> int:
        if tnid in self.textnorm_dict:
            return self.textnorm_dict[tnid]
        raise ValueError(f"The textnorm {tnid} is not in {list(self.textnorm_dict.keys())}")

    def read_tags(self, language_input, textnorm_input) -> tuple[list[int], list[int]]:
        """把 language/textnorm 参数解析成 onnx 需要的整数 id 列表。"""
        if isinstance(language_input, list):
            language_list = [self._get_lid(l) for l in language_input]
        elif isinstance(language_input, str):
            language_list = [self._get_lid(language_input)]
        else:
            raise ValueError(f"Unsupported type {type(language_input)} for language_input")

        if isinstance(textnorm_input, list):
            textnorm_list = [self._get_tnid(tn) for tn in textnorm_input]
        elif isinstance(textnorm_input, str):
            textnorm_list = [self._get_tnid(textnorm_input)]
        else:
            raise ValueError(f"Unsupported type {type(textnorm_input)} for textnorm_input")
        return language_list, textnorm_list

    def __call__(self, wav_content: str | np.ndarray | list[str], **kwargs) -> list[str]:
        language_input = kwargs.get("language", "auto")
        textnorm_input = kwargs.get("textnorm", "woitn")
        language_list, textnorm_list = self.read_tags(language_input, textnorm_input)

        waveform_list = self.load_data(wav_content, self.frontend.fs)
        waveform_nums = len(waveform_list)

        asr_res: list[str] = []
        for beg_idx in range(0, waveform_nums, self.batch_size):
            end_idx = min(waveform_nums, beg_idx + self.batch_size)
            feats, feats_len = self.extract_feat(waveform_list[beg_idx:end_idx])
            _language_list = language_list[beg_idx:end_idx]
            _textnorm_list = textnorm_list[beg_idx:end_idx]
            if not _language_list:
                _language_list = [language_list[0]]
                _textnorm_list = [textnorm_list[0]]
            B = feats.shape[0]
            if len(_language_list) == 1 and B != 1:
                _language_list = _language_list * B
            if len(_textnorm_list) == 1 and B != 1:
                _textnorm_list = _textnorm_list * B
            try:
                ctc_logits, encoder_out_lens = self.infer(
                    feats,
                    feats_len,
                    np.array(_language_list, dtype=np.int32),
                    np.array(_textnorm_list, dtype=np.int32),
                )
            except ONNXRuntimeError:
                logging.warning("input wav is silence or noise")
                continue
            for b in range(feats.shape[0]):
                x = ctc_logits[b, : int(encoder_out_lens[b]), :]
                yseq = np.argmax(x, axis=-1)
                # 连续去重（等价于 torch.unique_consecutive）
                mask = np.concatenate(([True], np.diff(yseq) != 0))
                yseq = yseq[mask]
                mask = yseq != self.blank_id
                token_int = yseq[mask].astype(int).tolist()
                asr_res.append(decode_ids(token_int, self.vocab))
        return asr_res

    def load_data(self, wav_content: str | np.ndarray | list[str], fs: int = None) -> list:
        if isinstance(wav_content, np.ndarray):
            return [wav_content]
        if isinstance(wav_content, str):
            return [load_wav(wav_content)]
        if isinstance(wav_content, (list, tuple)):
            return [load_wav(path) for path in wav_content]
        raise TypeError(f"The type of {wav_content} is not in [str, np.ndarray, list]")

    def extract_feat(self, waveform_list: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        feats, feats_len = [], []
        for waveform in waveform_list:
            speech, _ = self.frontend.fbank(waveform)
            if speech is None or speech.size == 0:
                raise ValueError("Empty speech detected, skipping this waveform.")
            feat, feat_len = self.frontend.lfr_cmvn(speech)
            feats.append(feat)
            feats_len.append(feat_len)

        feats = self.pad_feats(feats, np.max(feats_len))
        feats_len = np.array(feats_len).astype(np.int32)
        return feats, feats_len

    @staticmethod
    def pad_feats(feats: list[np.ndarray], max_feat_len: int) -> np.ndarray:
        def pad_feat(feat: np.ndarray, cur_len: int) -> np.ndarray:
            pad_width = ((0, max_feat_len - cur_len), (0, 0))
            return np.pad(feat, pad_width, "constant", constant_values=0)

        feat_res = [pad_feat(feat, feat.shape[0]) for feat in feats]
        return np.array(feat_res).astype(np.float32)

    def infer(
        self,
        feats: np.ndarray,
        feats_len: np.ndarray,
        language: np.ndarray,
        textnorm: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        return self.ort_infer([feats, feats_len, language, textnorm])
