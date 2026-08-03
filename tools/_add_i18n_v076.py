"""一次性补充 v0.7.6 的 i18n 键（zh_CN / zh_TW / en_US）。

- 关于页引擎「一键下载引擎与模型」按钮 + 不可下载原因
- FFmpeg 按钮文案改为「一键下载并安装」
- 放大设置各参数的帮助说明（engine.help.<key>）

用法：python tools/_add_i18n_v076.py
会就地更新 src/momentshift/i18n/locales/{loc}.json（含已存在键的改写）。
"""

from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALES = ["zh_CN", "zh_TW", "en_US"]
BASE = os.path.join(ROOT, "src", "momentshift", "i18n", "locales")

# (zh_CN, zh_TW, en_US)
KEYS: dict[str, tuple[str, str, str]] = {
    # ---- 关于页：引擎一键下载 ----
    "engine.download.oneclick": (
        "一键下载引擎与模型",
        "一鍵下載引擎與模型",
        "One-click download engine & model",
    ),
    "engine.reason.cuda": (
        "需要 NVIDIA CUDA 工具包，暂不支持一键下载，请手动安装后放入 tools/ 目录",
        "需要 NVIDIA CUDA 工具包，暫不支援一鍵下載，請手動安裝後放入 tools/ 目錄",
        "Requires the NVIDIA CUDA toolkit; one-click download is unsupported. "
        "Install manually into the tools/ folder.",
    ),
    "engine.reason.driver": (
        "为 NVIDIA 显卡驱动内置功能（Maxine），无需下载，请在驱动 / 控制面板中开启",
        "為 NVIDIA 顯示卡驅動內建功能（Maxine），無需下載，請在驅動 / 控制台中開啟",
        "Built into the NVIDIA display driver (Maxine); no download needed — "
        "enable it in the driver / control panel.",
    ),
    # ---- FFmpeg 按钮文案 ----
    "ffmpeg.download": (
        "一键下载并安装",
        "一鍵下載並安裝",
        "One-click download & install",
    ),
    # ---- 放大设置：各参数帮助 ----
    "engine.help.model": (
        "选择使用的模型 / 算法权重文件",
        "選擇使用的模型 / 演算法權重檔案",
        "Select the model / algorithm weights file to use.",
    ),
    "engine.help.tile": (
        "分块大小，0 为自动；过大会占用更多显存，过小则更慢",
        "分塊大小，0 為自動；過大會佔用更多顯存，過小則更慢",
        "Tile size; 0 = auto. Larger uses more VRAM, smaller is slower.",
    ),
    "engine.help.gpu": (
        "推理设备：auto 自动选用可用 GPU，cpu 仅使用处理器",
        "推理裝置：auto 自動選用可用 GPU，cpu 僅使用處理器",
        "Inference device: auto picks an available GPU; cpu uses the processor only.",
    ),
    "engine.help.tta": (
        "测试时增强（TTA）：可轻微提升画质，但速度明显变慢",
        "測試時增強（TTA）：可輕微提升畫質，但速度明顯變慢",
        "Test-time augmentation: slightly better quality but much slower.",
    ),
    "engine.help.jobs": (
        "线程 / 进程分配（格式 a:b:c），auto 按硬件自适应",
        "執行緒 / 程序分配（格式 a:b:c），auto 依硬體自適應",
        "Thread/process allocation (a:b:c); auto adapts to hardware.",
    ),
    "engine.help.noise": (
        "降噪强度，-1 表示不降噪",
        "降噪強度，-1 表示不降噪",
        "Denoise strength; -1 means no denoising.",
    ),
    "engine.help.scale": (
        "放大倍率，例如 2x、4x",
        "放大倍率，例如 2x、4x",
        "Upscale factor, e.g. 2x, 4x.",
    ),
    "engine.help.multiplier": (
        "插帧倍率：在原始帧率基础上倍增（2x = 每两帧补一帧）",
        "插幀倍率：在原始幀率基礎上倍增（2x = 每兩幀補一幀）",
        "Interpolation factor: multiplies the source frame rate (2x = one new frame between every two).",
    ),
    "engine.help.mode": (
        "处理模式：仅放大 / 仅降噪 / 二者兼顾",
        "處理模式：僅放大 / 僅降噪 / 二者兼顧",
        "Processing mode: upscale only / denoise only / both.",
    ),
    "engine.help.process": (
        "处理后端：GPU / cuDNN / CPU",
        "處理後端：GPU / cuDNN / CPU",
        "Backend: GPU / cuDNN / CPU.",
    ),
    "engine.help.crop_size": (
        "切块尺寸，分块处理以避免显存不足",
        "分塊尺寸，分塊處理以避免顯存不足",
        "Crop size for tiled processing to avoid running out of VRAM.",
    ),
    "engine.help.block_size": (
        "分块大小，0 为自动；越大越快但更占显存",
        "分塊大小，0 為自動；越大越快但更佔顯存",
        "Block size; 0 = auto. Larger is faster but uses more VRAM.",
    ),
    "engine.help.gpu_off": (
        "关闭 GPU 加速，改用 CPU（更慢但兼容性更好）",
        "關閉 GPU 加速，改用 CPU（更慢但相容性更好）",
        "Disable GPU acceleration and use the CPU (slower but more compatible).",
    ),
    "engine.help.acnet": (
        "启用 ACNet 细节增强模式",
        "啟用 ACNet 細節增強模式",
        "Enable the ACNet detail-enhancement mode.",
    ),
    "engine.help.zoom": (
        "放大倍数（可为小数，如 2.0）",
        "放大倍數（可為小數，如 2.0）",
        "Zoom factor (can be fractional, e.g. 2.0).",
    ),
    "engine.help.hdn": (
        "启用 HDN 高清降噪",
        "啟用 HDN 高畫質降噪",
        "Enable HDN high-quality denoising.",
    ),
    "engine.help.hdn_level": (
        "HDN 降噪等级（1~3）",
        "HDN 降噪等級（1~3）",
        "HDN denoise level (1–3).",
    ),
    "engine.help.passes": (
        "处理遍数，多次叠加可增强效果但更慢",
        "處理遍數，多次疊加可增強效果但更慢",
        "Number of passes; more passes improve quality but slow things down.",
    ),
    "engine.help.push_color": (
        "色彩推进强度",
        "色彩推進強度",
        "Color push strength.",
    ),
    "engine.help.strength_color": (
        "颜色增强强度（0~1）",
        "顏色增強強度（0~1）",
        "Color enhancement strength (0–1).",
    ),
    "engine.help.strength_gradient": (
        "边缘 / 梯度增强强度（0~1）",
        "邊緣 / 梯度增強強度（0~1）",
        "Edge / gradient enhancement strength (0–1).",
    ),
    "engine.help.gpu_on": (
        "启用 GPU 加速（关闭则使用 CPU）",
        "啟用 GPU 加速（關閉則使用 CPU）",
        "Enable GPU acceleration (off uses the CPU).",
    ),
    "engine.help.uhd": (
        "启用 UHD 超高清模式，提升大分辨率处理质量",
        "啟用 UHD 超高畫質模式，提升大解析度處理品質",
        "Enable UHD mode for better quality on large resolutions.",
    ),
    "engine.help.tta_temporal": (
        "时序 TTA：跨帧联合增强，提升视频稳定性",
        "時序 TTA：跨幀聯合增強，提升影片穩定性",
        "Temporal TTA: joint cross-frame enhancement for steadier video.",
    ),
    "engine.help.syncgap": (
        "帧对齐同步间隙参数，影响多帧对齐精度",
        "幀對齊同步間隙參數，影響多幀對齊精度",
        "Frame-alignment sync-gap parameter affecting multi-frame alignment.",
    ),
}


def main() -> None:
    for loc in LOCALES:
        path = os.path.join(BASE, f"{loc}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        idx = LOCALES.index(loc)
        added = 0
        for key, tup in KEYS.items():
            val = tup[idx]
            if data.get(key) != val:
                data[key] = val
                added += 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"[{loc}] updated {added} keys (total {len(data)})")


if __name__ == "__main__":
    main()
