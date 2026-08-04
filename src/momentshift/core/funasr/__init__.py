# Copyright FunASR (https://github.com/alibaba-damo-academy/FunASR). All Rights Reserved.
#  MIT License  (https://opensource.org/licenses/MIT)
#
# MomentShift 内置 FunASR 推理（裁剪自 funasr_onnx 0.4.2）。
# 只保留 Paraformer 字级中文模型所需能力；依赖收敛为 numpy/onnxruntime/jieba。
"""MomentShift 内置 FunASR 推理子包。"""

from .paraformer_bin import Paraformer

__all__ = ["Paraformer"]
