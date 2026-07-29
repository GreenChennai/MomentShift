# MomentShift · 瞬变工坊

> 傻瓜式 ffmpeg 多媒体格式转换工具 —— 拖进去，选格式，批量转。

MomentShift（中文名 **瞬变工坊**）是一个基于 [ffmpeg](https://ffmpeg.org/) 的跨平台多媒体格式转换工具。
我们把 ffmpeg 复杂的参数封装成「最优质量、写死即用」的预设，让用户无需理解任何命令行，
只要把图片 / 音频 / 视频拖进窗口、点一下目标格式，就能批量、多线程地完成转换。

> 文件格式转换只是 MomentShift 的**第一个大功能模块**。后续会在此架构上继续扩展更多能力，
> 当前版本先专注把「转换」做到极致。

---

## ✨ 特性

- **傻瓜式操作**：内置最优质量参数（已写死），无需手动调参。
- **拖拽即转**：支持把文件直接拖进窗口，或点击选择。
- **批量队列**：多线程队列并发转换，显著加速大批量任务。
- **CPU / GPU 加速**：自动检测 NVIDIA (nvenc) / Intel (qsv) / AMD (amf) / Apple (videotoolbox) 硬件编码器，自动回退 CPU。
- **多格式互转**：图片（png/jpg/webp/bmp/tiff/gif）、音频（mp3/wav/flac/aac/m4a/ogg）、视频（mp4/mkv/mov/webm/avi/gif）。
- **国际化 (i18n)**：简体中文 / 繁体中文 / English，语言文件为独立 JSON，易于修改。
- **跨平台前瞻**：代码层面不绑定 Windows，已为 macOS / Linux 发行预留构建矩阵。
- **CI 自动构建**：本地不编译成品，由 GitHub Actions 完成 PyInstaller 打包与 Release 发行。

---

## 🖥️ 平台

| 平台      | 状态        | 说明                                   |
| --------- | ----------- | -------------------------------------- |
| Windows   | ✅ 首发      | 当前主要支持目标                       |
| macOS     | 🔜 预留      | CI 矩阵已留位，待验证                  |
| Linux     | 🔜 预留      | CI 矩阵已留位，待验证                  |

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

构建 / 发行产物由 **GitHub Actions** 自动完成，详见 `.github/workflows/build.yml`。

---

## 📦 打包与发行（CI）

代码提交后推送 Tag 即可触发自动构建与发行：

```bash
git tag v0.1.0
git push origin v0.1.0
```

CI 会：

1. 拉取对应平台的 ffmpeg 静态二进制（`tools/download_ffmpeg.py`）；
2. 用 PyInstaller（`build.spec`）打包为单目录可执行程序；
3. 将 ffmpeg 一并放入产物目录；
4. 在 GitHub Release 上传压缩包。

---

## 📁 目录结构

```
MomentShift/
├── .github/workflows/build.yml   # CI 自动构建 / 发行
├── build.spec                     # PyInstaller 配置
├── tools/download_ffmpeg.py       # 跨平台 ffmpeg 下载器
├── src/momentshift/
│   ├── core/                      # 与 UI 无关的转换引擎
│   │   ├── ffmpeg.py              # ffmpeg 发现 / 版本 / 编码器解析
│   │   ├── hardware.py            # CPU/GPU 硬件加速检测
│   │   ├── presets.py             # 格式预设 + 最优参数（写死）
│   │   ├── converter.py           # 单任务 ffmpeg 执行 + 进度
│   │   └── queue.py               # 多线程队列管理器
│   ├── gui/                       # PyQt6 + Fluent Widgets 界面
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

> ⚠️ 注意：GUI 依赖 [PyQt6](https://www.riverbankcomputing.com/) 与
> [PyQt6-Fluent-Widgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)（均为 GPLv3）。
> 因此**分发二进制成品时必须遵循 GPLv3**。若未来需要闭源 / 商业发行，
> 可考虑迁移到 LGPL 的 **PySide6 + PySide6-Fluent-Widgets**（API 基本一致，
> 本项目已将全部 Qt 导入集中在 `core/qt_compat.py`，便于切换）。

---

## 🤝 贡献

欢迎提交 Issue 与 Pull Request。代码风格遵循 PEP 8，核心转换逻辑与 UI 解耦，
便于单元测试与持续集成。
