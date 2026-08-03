"""一次性脚本：为 v0.7.5 引擎重构补齐三语 i18n 键。"""

import json
from pathlib import Path

LOC = Path(__file__).resolve().parents[1] / "src/momentshift/i18n/locales"

KEYS = {
    # ---------------- 引擎卡片 ----------------
    "engine.card.title": (
        "超分辨率 / 插帧引擎",
        "超解析度 / 插幀引擎",
        "Upscaling & Interpolation Engines",
    ),
    "engine.card.hint": (
        "引擎与算法不随软件分发。点击「前往下载」到官网获取对应压缩包，"
        "解压后把内容放进 tools/<引擎名> 文件夹，再点「重新检测」即可启用。",
        "引擎與演算法不隨軟體分發。點擊「前往下載」到官網取得對應壓縮檔，"
        "解壓後把內容放進 tools/<引擎名> 資料夾，再點「重新偵測」即可啟用。",
        'Engines are not bundled with the app. Use "Download page" to get the '
        'archive, extract it into tools/<engine-id>, then hit "Rescan".',
    ),
    "engine.card.summary": (
        "已就绪：超分 {sr} 个 · 插帧 {interp} 个",
        "已就緒：超解析 {sr} 個 · 插幀 {interp} 個",
        "Ready: {sr} upscaler(s) · {interp} interpolator(s)",
    ),
    "engine.group.sr": ("超分辨率", "超解析度", "Super Resolution"),
    "engine.group.interp": ("插帧", "插幀", "Frame Interpolation"),
    "engine.goto_download": ("前往下载", "前往下載", "Download page"),
    "engine.open_folder": ("打开文件夹", "開啟資料夾", "Open folder"),
    "engine.open_root": ("打开 tools 目录", "開啟 tools 目錄", "Open tools folder"),
    "engine.rescan": ("重新检测", "重新偵測", "Rescan"),
    "engine.status.ready": ("已就绪", "已就緒", "Ready"),
    "engine.status.missing": ("未安装", "未安裝", "Not installed"),
    "engine.status.driver": ("需驱动支持", "需驅動支援", "Driver-level"),
    # ---------------- 参数标签 ----------------
    "engine.param.model": ("模型", "模型", "Model"),
    "engine.param.scale": ("放大倍率", "放大倍率", "Scale"),
    "engine.param.zoom": ("放大倍率", "放大倍率", "Zoom factor"),
    "engine.param.noise": ("降噪等级", "降噪等級", "Denoise level"),
    "engine.param.tile": ("分块大小", "分塊大小", "Tile size"),
    "engine.param.gpu": ("硬件加速", "硬體加速", "GPU device"),
    "engine.param.gpu_on": ("启用 GPU", "啟用 GPU", "Enable GPU"),
    "engine.param.gpu_off": ("禁用 GPU", "停用 GPU", "Disable GPU"),
    "engine.param.tta": ("TTA 增强", "TTA 增強", "TTA mode"),
    "engine.param.tta_temporal": ("时域 TTA", "時域 TTA", "Temporal TTA"),
    "engine.param.jobs": ("线程配置", "執行緒配置", "Thread config"),
    "engine.param.syncgap": ("拼接同步", "拼接同步", "Sync gap"),
    "engine.param.mode": ("处理模式", "處理模式", "Mode"),
    "engine.param.process": ("计算设备", "計算裝置", "Processor"),
    "engine.param.crop_size": ("切块尺寸", "切塊尺寸", "Crop size"),
    "engine.param.block_size": ("分块尺寸", "分塊尺寸", "Block size"),
    "engine.param.acnet": ("ACNet 模式", "ACNet 模式", "ACNet mode"),
    "engine.param.hdn": ("HDN 降噪", "HDN 降噪", "HDN denoise"),
    "engine.param.hdn_level": ("HDN 等级", "HDN 等級", "HDN level"),
    "engine.param.passes": ("处理遍数", "處理遍數", "Passes"),
    "engine.param.push_color": ("推色次数", "推色次數", "Push color count"),
    "engine.param.strength_color": ("推色强度", "推色強度", "Color strength"),
    "engine.param.strength_gradient": ("梯度强度", "梯度強度", "Gradient strength"),
    "engine.param.multiplier": ("插帧倍率", "插幀倍率", "Interpolation ratio"),
    "engine.param.uhd": ("UHD 模式", "UHD 模式", "UHD mode"),
    # ---------------- 候选项 ----------------
    "engine.opt.auto": ("自动", "自動", "Auto"),
    "engine.opt.cpu": ("CPU", "CPU", "CPU"),
    "engine.opt.noise_off": ("关闭", "關閉", "Off"),
    "engine.opt.balanced": ("均衡", "均衡", "Balanced"),
    "engine.opt.noise_scale": ("降噪 + 放大", "降噪 + 放大", "Denoise + Scale"),
    "engine.opt.scale_only": ("仅放大", "僅放大", "Scale only"),
    "engine.opt.noise_only": ("仅降噪", "僅降噪", "Denoise only"),
    "engine.opt.jpeg_friendly": ("JPEG 友好", "JPEG 友善", "JPEG-friendly"),
    "engine.opt.standard": ("标准", "標準", "Standard"),
    "engine.opt.syncgap_off": ("关闭", "關閉", "Off"),
    # ---------------- 引擎说明 ----------------
    "engine.desc.realesrgan-ncnn-vulkan": (
        "通用真实世界超分，照片与插画都能胜任。官方压缩包自带 4 个模型，"
        "Vulkan 加速兼容 N/A/I 三家显卡，是最省心的首选。",
        "通用真實世界超解析，照片與插畫皆可勝任。官方壓縮檔自帶 4 個模型，"
        "Vulkan 加速相容 N/A/I 三家顯示卡，是最省心的首選。",
        "General-purpose real-world upscaler for both photos and artwork. The "
        "official archive ships 4 models; Vulkan runs on any modern GPU.",
    ),
    "engine.desc.waifu2x-ncnn-vulkan": (
        "经典二次元超分，降噪与放大可分别调节。CUnet 模型质量最好，"
        "UpConv7 更快，支持最高 32 倍链式放大。",
        "經典二次元超解析，降噪與放大可��別調節。CUnet 模型品質最好，"
        "UpConv7 更快，支援最高 32 倍鏈式放大。",
        "The classic anime upscaler with independent denoise and scale控制. "
        "CUnet gives the best quality, UpConv7 is faster; up to 32x.",
    ),
    "engine.desc.waifu2x-caffe": (
        "Windows 老牌 Waifu2x 实现，依赖 NVIDIA cuDNN，命令行程序为 "
        "waifu2x-caffe-cui.exe。模型选择最丰富，但仅限 N 卡。",
        "Windows 老牌 Waifu2x 實作，依賴 NVIDIA cuDNN，命令列程式為 "
        "waifu2x-caffe-cui.exe。模型選擇最豐富，但僅限 N 卡。",
        "The long-standing Windows Waifu2x build (waifu2x-caffe-cui.exe). "
        "Widest model selection, but requires an NVIDIA GPU with cuDNN.",
    ),
    "engine.desc.waifu2x-converter": (
        "基于 OpenCV / OpenCL 的 Waifu2x 实现，可在无独立显卡的机器上跑 CPU 模式，"
        "支持任意小数倍率。",
        "基於 OpenCV / OpenCL 的 Waifu2x 實作，可在無獨立顯示卡的機器上跑 CPU 模式，"
        "支援任意小數倍率。",
        "OpenCV/OpenCL Waifu2x port. Runs on CPU-only machines and accepts "
        "arbitrary fractional scale ratios.",
    ),
    "engine.desc.srmd-ncnn-vulkan": (
        "支持 0–10 级连续可调降噪的通用超分，对压缩噪点、轻度模糊的老素材"
        "特别有效，速度慢于 Real-ESRGAN。",
        "支援 0–10 級連續可調降噪的通用超解析，對壓縮雜訊、輕度模糊的老素材"
        "特別有效，速度慢於 Real-ESRGAN。",
        "General upscaler with a 0–10 continuous denoise dial. Great for noisy "
        "or slightly blurred legacy footage, slower than Real-ESRGAN.",
    ),
    "engine.desc.srmd-cuda": (
        "SRMD 的 CUDA 版本，只支持 NVIDIA 显卡，同等参数下比 Vulkan 版更快，"
        "需要正确安装 CUDA 运行库。",
        "SRMD 的 CUDA 版本，只支援 NVIDIA 顯示卡，同等參數下比 Vulkan 版更快，"
        "需要正確安裝 CUDA 執行庫。",
        "CUDA build of SRMD. NVIDIA-only, faster than the Vulkan build at the "
        "same settings, requires a working CUDA runtime.",
    ),
    "engine.desc.realsr-ncnn-vulkan": (
        "面向真实照片退化训练的 4 倍超分。DF2K_JPEG 模型对 JPEG 压缩痕迹"
        "更宽容，适合处理网络流传的低质图片。",
        "面向真實照片退化訓練的 4 倍超解析。DF2K_JPEG 模型對 JPEG 壓縮痕跡"
        "更寬容，適合處理網路流傳的低品質圖片。",
        "4x upscaler trained on real-world photo degradation. The DF2K_JPEG "
        "model tolerates JPEG artifacts well.",
    ),
    "engine.desc.realcugan-ncnn-vulkan": (
        "二次元超分新锐，细节保留优于 Waifu2x，支持 1–4 倍与 -1–3 级降噪；"
        "syncgap 用于消除分块拼接痕迹。",
        "二次元超解析新銳，細節保留優於 Waifu2x，支援 1–4 倍與 -1–3 級降噪；"
        "syncgap 用於消除分塊拼接痕跡。",
        "Modern anime upscaler that preserves more detail than Waifu2x. 1–4x "
        "scale, -1–3 denoise; sync-gap removes tile seams.",
    ),
    "engine.desc.anime4kcpp": (
        "同时提供 Anime4K 与 ACNet 两套算法：Anime4K 走锐化管线，速度极快可实时；"
        "ACNet 走卷积网络，质量更高。HDN 用于强降噪。",
        "同時提供 Anime4K 與 ACNet 兩套演算法：Anime4K 走銳化管線，速度極快可即時；"
        "ACNet 走卷積網路，品質更高。HDN 用於強降噪。",
        "Ships both Anime4K (ultra-fast sharpening pipeline, real-time capable) "
        "and ACNet (CNN-based, higher quality). HDN adds strong denoising.",
    ),
    "engine.desc.rtx-super-resolution": (
        "NVIDIA 驱动级视频超分，需要 RTX 20 系及以上显卡。它没有独立命令行程序，"
        "本软件无法直接调用；请下载 NVIDIA Maxine Video Effects SDK 的示例程序"
        "（UpscalePipelineApp.exe）放入本文件夹后使用。",
        "NVIDIA 驅動級影片超解析，需要 RTX 20 系及以上顯示卡。它沒有獨立命令列程式，"
        "本軟體無法直接呼叫；請下載 NVIDIA Maxine Video Effects SDK 的範例程式"
        "（UpscalePipelineApp.exe）放入本資料夾後使用。",
        "NVIDIA driver-level video super resolution (RTX 20-series or newer). "
        "It has no standalone CLI; drop the Maxine Video Effects SDK sample "
        "(UpscalePipelineApp.exe) into this folder to use it.",
    ),
    "engine.desc.rife-ncnn-vulkan": (
        "目前最快最稳的实时插帧算法，v4.6 综合表现最佳，适合绝大多数视频补帧；"
        "UHD 模式用于 4K 以上素材。",
        "目前最快最穩的即時插幀演算法，v4.6 綜合表現最佳，適合絕大多數影片補幀；"
        "UHD 模式用於 4K 以上素材。",
        "The fastest and most reliable real-time interpolator. v4.6 is the best "
        "all-rounder; enable UHD mode for 4K+ sources.",
    ),
    "engine.desc.cain-ncnn-vulkan": (
        "通道注意力插帧网络，对动漫大位移场景的鬼影控制较好，但只支持固定 2 倍链式插帧。",
        "通道注意力插幀網路，對動漫大位移場景的鬼影控制較好，但只支援固定 2 倍鏈式插幀。",
        "Channel-attention interpolation with fewer ghosting artifacts on "
        "large anime motion. Fixed 2x steps only.",
    ),
    "engine.desc.dain-ncnn-vulkan": (
        "基于深度感知的插帧算法，遮挡处理最好、质量最高，"
        "但显存占用与耗时明显大于 RIFE，建议配合分块使用。",
        "基於深度感知的插幀演算法，遮擋處理最好、品質最高，"
        "但顯示記憶體佔用與耗時明顯大於 RIFE，建議搭配分塊使用。",
        "Depth-aware interpolation with the best occlusion handling and quality, "
        "but far heavier on VRAM and time than RIFE. Use tiling.",
    ),
    "engine.desc.ifrnet-ncnn-vulkan": (
        "轻量高效的插帧网络，速度与质量平衡好。Vimeo90K 为通用模型，GoPro 更适合高速运动素材。",
        "輕量高效的插幀網路，速度與品質平衡好。Vimeo90K 為通用模型，GoPro 更適合高速運動素材。",
        "Lightweight, efficient interpolation with a good speed/quality "
        "balance. Vimeo90K is general purpose; GoPro suits fast motion.",
    ),
    # ---------------- 放大界面 ----------------
    "upscale.engine.none": (
        "无模型 / 算法可用，请下载",
        "無模型 / 演算法可用，請下載",
        "No model / algorithm available, please download",
    ),
    "upscale.engine.none_hint": (
        "尚未检测到任何超分辨率或插帧引擎。请前往「关于」页查看全部支持的"
        "引擎与算法，下载后放入对应的 tools 文件夹即可启用。",
        "尚未偵測到任何超解析度或插幀引擎。請前往「關於」頁查看全部支援的"
        "引擎與演算法，下載後放入對應的 tools 資料夾即可啟用。",
        "No super-resolution or interpolation engine detected. Open the About "
        "page to see every supported engine, then drop it into its tools folder.",
    ),
    "upscale.engine.detect": ("检测环境", "偵測環境", "Check environment"),
}


def main() -> None:
    for idx, loc in enumerate(("zh_CN", "zh_TW", "en_US")):
        path = LOC / f"{loc}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        added = 0
        for key, vals in KEYS.items():
            if key not in data:
                added += 1
            data[key] = vals[idx]
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{loc}: +{added} keys, total {len(data)}")


if __name__ == "__main__":
    main()
