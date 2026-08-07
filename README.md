
<p align="center">
  <img src="src/momentshift/resources/icons/app_logo.png" alt="MomentShift Logo" width="132">
</p>
<h1 align="center">
  MomentShift · 瞬变工坊
</h1>
<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL--3.0-blue.svg" alt="License: GPL-3.0"></a>
  <img src="https://img.shields.io/badge/version-0.9.0--fc97100-brightgreen.svg" alt="Version 0.9.0-fc97100">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/UI-PyQt6%20%2B%20Fluent--Widgets-238636.svg" alt="UI">
</p>
<p align="center">
  <strong>傻瓜式 ffmpeg 多媒体处理工具 —— 拖进去，选格式，批量转；本地跑图片/视频的超分和插帧；视频/音频提取文字</strong>
</p>

---

<p align="center">
  <strong>声明：本项目全部代码由 AI（人工智能）编写，用于个人技术能力提升与学习交流，并非由人工逐行手写。如有问题或改进建议，欢迎提 Issue / PR</strong>
</p>

---

## ✨ 特性

- **傻瓜式操作**：内置最优质量参数（已写死），无需手动调参。
- **拖拽即转**：支持把文件直接拖进窗口，或点击选择。
- **批量队列**：多线程队列并发处理，显著加速大批量任务。
- **硬件加速**：自动检测 NVIDIA (nvenc) / Intel (qsv) / AMD (amf) / Apple (videotoolbox) 编码器自动回退 CPU；放大 / 插帧引擎走 ncnn-vulkan（Vulkan / CUDA）。
- **模型管理**：ASR 模型与放大 / 插帧引擎均支持「一键下载」（HuggingFace / 镜像源），按硬件门控（NVIDIA CUDA、内存）智能灰显不可用项。
- **服务模式**：音频转文字可作为本地服务，其他应用经 `http://127.0.0.1:<端口>/v1` 以 OpenAI 兼容接口调用。
- **系统托盘**：关闭窗口时最小化到托盘静默运行，双击托盘图标立即唤起。
- **静默任务执行**：使用"快速调用"来传递任务时，后台静默运行。
- **国际化 (i18n)**：简体中文 / 繁体中文 / English，语言文件为独立 JSON，易于修改。
- **跨平台**：Windows 首发，macOS / Linux 已从源码运行并补齐引擎 / 工具的多平台下载源。
- **转换进度与大小对比**：实时进度条 + 百分比，处理完成后对比前后文件大小，结果路径可一键复制。
- **放大前后对比窗口**：图片 / 视频 / GIF 对比查看——叠放分割（分割线随鼠标移动）/ 左右并排双模式、**Ctrl+滚轮整体缩放**看细节（缩放时自动暂停视频、停止后恢复）、视频 / GIF **左右播放同步**、背景水印标注「原媒体展示 / 处理后媒体展示」。

---

## 🧩 功能模块

### 1. 转换（ffmpeg 格式互转）
把图片 / 音频 / 视频 格式互转。
自动选择最优编码器与硬件加速，批量队列并发；
处理完成后显示前后文件大小对比，结果路径可一键复制。

### 2. 压缩（图片 / 音频 / 视频）
内置多后端压缩引擎，**按文件类型路由**（拖入文件后按类型分别弹出「创建压缩任务」窗口，设置冻结进每个任务）：
- **PNG**：oxipng 无损最优（可切 Pillow / FFmpeg）；
- **JPG**：jpegoptim 无损 / 有损（可切 Pillow / FFmpeg）；
- **GIF**：Gifsicle GIF 动图优化；
- **其他图片**：Pillow 高质量重编码（可切 FFmpeg）；
- **视频 / 音频**：FFmpeg 压缩（视频/音频独立参数面板 + 质量预设）。
默认走无损最优策略，无需调参；「压缩设置」已并入创建压缩任务窗口，
主界面保留「输出位置」并与弹窗内保存位置双向同步。

### 3. 放大 / 插帧（AI）
基于 **ncnn-vulkan** 的离线 AI 引擎，无需联网即可推理：
- **超分辨率**：Real-ESRGAN、Waifu2x（ncnn / caffe / converter）、Real-CUGAN、AnimeSharp 等；
- **插帧**：RIFE 等；
- 「关于」页的**引擎卡**展示每个引擎的状态，「一键下载」自动获取对应平台二进制；
- **放大前后对比**：任务完成后可直接打开对比窗口，叠放分割 / 左右并排查看放大效果，Ctrl+滚轮缩放细节，视频 / GIF 左右同步播放；
- **任意尺寸安全**：自动探测输入尺寸并做 tile 对齐（pad→引擎→裁剪），避免 ncnn 分块拼接错位；图片用预缩放解码，超大图也能流畅查看。

### 4. 音频转文字（本地 ASR）
基于 [FunASR](https://github.com/modelscope/FunASR) 的本地语音识别：
- **主要模型**：Paraformer-large-zh / SenseVoice-Small 等可本地推理的 ASR 模型；
- **可选功能模型**：FSMN-VAD（语音活动检测）、CAM++（说话人分离）、ct-punc（标点恢复，CPU 可用）、emotion2vec（情感识别，需 NVIDIA CUDA + 完整 funasr 包）；
- **模型管理**：按硬件门控（NVIDIA CUDA、内存）灰显不可用模型，「一键下载」从 HuggingFace / 镜像源获取；
- **服务模式**：将本软件作为本地 ASR 服务器，其他应用经 `http://127.0.0.1:<端口>/v1/audio/transcriptions` 以 **OpenAI 兼容接口**调用，支持 Bearer 鉴权。

### 5. 快速调用 (1/2/3)
- **右键调用**：通过右键图片/视频/音频 可以快速调用软件的 **转换**/**压缩**/**转换**/**放大** 功能；
- **静默运行**：调用之后，任务会直接传递到软件的任务队列中，并自动开始运行，开始和完成时会有系统提示+音效；
- **高效多任务**：能够批量选择批量运行任务；
- **高效使用**：不用频繁打开软件操作，右键即刻运行；

---

## 🖥️ 平台

| 平台    | 状态   | 说明                                     |
| ------- | ------ | ---------------------------------------- |
| Windows | ✅ 首发 | 当前主要支持目标（含系统托盘、右键菜单） |
| macOS   | ✅ 可用 | 从源码运行，引擎 / 工具补三平台下载源    |
| Linux   | ✅ 可用 | 从源码运行，引擎 / 工具补三平台下载源    |

> 注：右键「快速调用」菜单为 Windows 专属功能；其余能力跨平台一致。

---

## 🚀 快速开始（开发）

需要 Python 3.10+。

```bash
# 1. 克隆仓库
git clone git@github.com:GreenChennai/MomentShift.git
cd MomentShift

# 2. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
pip install -e .              # 可命令行直接 `momentshift` 启动

# 3. 运行（确保系统已安装 ffmpeg，或放在可执行文件同级目录）
python -m momentshift
```

---

## 📦 构建与发行

- **本地构建**：用 PyInstaller 按 `build.spec` 打包为单目录（onedir）程序：

  ```bash
  .venv\Scripts\python.exe -m PyInstaller build.spec --noconfirm --distpath dist --workpath build
  ```

  打包产物为 `dist/MomentShift/`，内含 `momentshift.exe` 与依赖。
- **CI 自动发行**：推送带 `v*` 前缀的 Tag 即触发 `.github/workflows/build.yml` 自动构建并发布 GitHub Release。

> **关于 ffmpeg**：本软件默认**不内置 ffmpeg**（保证安装包小巧）。首次使用会在开始界面检测 ffmpeg：
> - 已放置 `ffmpeg.exe` / `ffprobe.exe` 到软件安装根目录 → 直接使用；
> - 未检测到 → 点击界面上的「一键下载并安装」按钮，或前往 [ffmpeg 官网下载页](https://ffmpeg.org/download.html) 手动下载后放到安装根目录；
> - 也可在「设置 → ffmpeg 来源」中选择仅使用系统 `PATH` 中的 ffmpeg。

---

## 📁 目录结构

```
MomentShift/
├── .github/workflows/build.yml   # CI 自动构建 / 发行
├── build.spec                     # PyInstaller 配置
├── tools/                         # ffmpeg 下载器、模型归位等工具脚本
├── src/momentshift/
│   ├── core/                      # 与 UI 无关的核心逻辑
│   │   ├── ffmpeg.py              # ffmpeg 发现 / 版本 / 编码器解析
│   │   ├── hardware.py           # CPU/GPU 硬件加速检测
│   │   ├── presets.py            # 格式预设 + 最优参数（写死）
│   │   ├── converter.py          # 单任务 ffmpeg 执行 + 进度
│   │   ├── compressor.py         # 图片压缩引擎（oxipng / jpegoptim / Pillow）
│   │   ├── upscaler.py           # AI 超分 / 插帧引擎调度
│   │   ├── engines.py            # 放大 / 插帧引擎注册表（含多平台下载源）
│   │   ├── engine_download.py    # 引擎一键下载（流式进度）
│   │   ├── asr_worker.py         # ASR 转写 worker（逐段 + 标点 / 情感）
│   │   ├── funasr_engine.py      # FunASR 本地推理封装（模型 / VAD / 标点 / 情感）
│   │   ├── asr_server.py         # 本地 ASR 服务模式（OpenAI 兼容）
│   │   ├── queue.py              # 多线程队列管理器
│   │   └── advanced.py           # 高级参数构建
│   ├── gui/                       # PyQt6 + Fluent Widgets 界面
│   │   ├── main_window.py        # 主窗口 + 系统托盘 + 启动屏
│   │   ├── splash.py             # 启动屏（Logo + 进度条）
│   │   ├── convert_interface.py   # 转换界面（输入 + 队列）
│   │   ├── convert_setup_dialog.py # 转换设置弹窗
│   │   ├── compress_interface.py  # 压缩界面
│   │   ├── upscale_interface.py   # 放大 / 插帧界面
│   │   ├── asr_interface.py       # 音频转文字（模型管理 / ASR 设置 / 服务模式）
│   │   ├── engine_card.py        # 关于页引擎卡（一键下载 + 进度）
│   │   ├── ffmpeg_card.py        # ffmpeg 一键下载卡
│   │   └── setting_interface.py   # 设置（语言 / 主题 / 硬件 / 托盘等）
│   ├── i18n/locales/              # zh_CN.json / zh_TW.json / en_US.json
│   └── __main__.py                # 入口
└── requirements.txt
```

---

## 🌐 国际化

语言文件位于 `src/momentshift/i18n/locales/`，采用 **JSON** 格式，便于修改与读取：

- `zh_CN.json` —— 简体中文
- `zh_TW.json` —— 繁体中文
- `en_US.json` —— English

新增语言只需复制一份 JSON 并在 `translator.py` 的 `SUPPORTED_LOCALES` 注册即可。

---

## 📜 授权 License

本项目以 **GPL-3.0-or-later** 开源。

---

## 📝 日志

软件在**程序根目录下的 `logs/`** 自动记录运行日志（按天新建、`DEBUG` 级落盘，单文件超过 8 MB 自动滚动出 `.1`/`.2`/`.3` 备份、最多保留 4 份约 32 MB，并仅保留最近 7 天的文件，启动时自动清理）。
转换失败、异常、闪退等信息都会写入 `logs/momentshift-YYYY-MM-DD.log`，便于排查问题。
如遇异常，请将对应的日志文件随 Issue 一并提交。

## 🙏 致谢 / Acknowledgements

本项目在开发与设计中参考、使用了以下优秀的开源项目与资源，在此表示衷心感谢：

| 项目                                        | 作者 / 维护者               | 官网 / 仓库                                    | 用途                                   |
| ------------------------------------------- | --------------------------- | ---------------------------------------------- | -------------------------------------- |
| **ffmpeg**                                  | FFmpeg 团队                 | https://ffmpeg.org/                            | 多媒体转码核心引擎                     |
| **FunASR**                                  | 阿里巴巴达摩院 (ModelScope) | https://github.com/modelscope/FunASR           | 本地语音识别推理                       |
| **PyQt6**                                   | Riverbank Computing         | https://www.riverbankcomputing.com/            | Python GUI 框架                        |
| **PyQt6-Fluent-Widgets** (`qfluentwidgets`) | zhiyiYo                     | https://github.com/zhiyiYo/PyQt-Fluent-Widgets | Fluent 风格 UI 组件库                  |
| **Real-ESRGAN / ncnn-vulkan**               | nihui                       | https://github.com/nihui                       | AI 超分辨率引擎与 Vulkan 推理          |
| **RIFE**                                    | megvii-research / nihui 等  | https://github.com/nihui                       | 插帧引擎                               |
| **FFmpegFreeUI**（参考）                    | 1059 Studio (Lake1059)      | https://github.com/Lake1059/FFmpegFreeUI       | ffmpeg 调用参数与交互设计的参考（MIT） |

特别感谢以下 **ffmpeg 静态构建下载源**，本软件的「一键下载」功能即从中获取对应平台的二进制：

- Windows：[gyan.dev](https://www.gyan.dev/ffmpeg/builds/)
- macOS：[evermeet.cx](https://evermeet.cx/ffmpeg/)
- Linux：[johnvansickle.com](https://johnvansickle.com/ffmpeg/)

> ffmpeg 及其静态构建由第三方提供，遵循各自的开源许可；本软件仅对其作调用封装，相关权利归原作者所有。

> 如有遗漏未标注的开源项目请提醒 :)

## 🤝 贡献

欢迎提交 Issue 与 Pull Request。代码风格遵循 PEP 8，核心逻辑与 UI 解耦，便于单元测试与持续集成。
