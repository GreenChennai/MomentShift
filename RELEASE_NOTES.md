# MomentShift V0.9.1 测试版

> 傻瓜式 ffmpeg 多媒体处理工具 —— 转换 / 压缩 / AI 放大插帧 / 音频转文字，全部本地运行。

## 🎯 本版重点（V0.9.0 → V0.9.1）

### FFmpeg 一键下载彻底修复
- **根因**：旧版单源（gyan.dev，国内常被墙）、阻塞读取、无进度 / 重试 / 备用源 / 友好报错 —— 大部分用户卡在下载。
- **新机制**（`core/ffmpeg_download.py` 重写）：
  - **多镜像有序回退**：ghproxy.net 包 BtbN latest → gyan.dev essentials → 直连 GitHub BtbN（Windows）；macOS / Linux 维持原源。
  - **流式下载 + 进度**：256KB 分块、实时回调、有总大小显示百分比，否则不确定态。
  - **容错**：每源重试 2 次、30s 套接字超时、下载后解压并校验 ffmpeg / ffprobe 非空。
  - **友好报错**：HTTP / 超时 / DNS / 损坏包分类提示，并提示「可手动放 ffmpeg.exe / ffprobe.exe 到软件目录」。
- 卡片 UI 接线进度条与失败说明（`gui/ffmpeg_card.py`），三语文案新增 `ffmpeg.failed_detail` / `ffmpeg.manual_hint`。

### 主题色对齐 GitHub 官方调色板（仅换色，不改布局 / 功能）
- 主色 `#238636 → #1F883D`、成功绿 `#1A7F37`、危险红 `#CF222E`、链接 / 进度蓝 `#0969DA`。
- 边框 `#E0E0E0 → #D0D7DE`、表面 `#F5F5F5 → #F6F8FA`、悬停 `#F3F4F6`、按下 `#EBECF0`。
- 文字 fg `#1F2328` / 次要 `#656D76` / 弱化 `#57606A`。
- 视觉零差异，令牌集中管理（`gui/tokens.py`），改一处即全局换色。

## 📦 安装
- **绿色版**：下载 `MomentShift-Windows-Portable.zip` 解压即用（首次启动按提示下载 ffmpeg，或手动放入安装目录）。
- **安装包**：`MomentShift-Windows-Setup.exe`（NSIS 安装向导）。

## ⚠️ 测试版说明
- 本版本为测试版（prerelease），主要验证 FFmpeg 一键下载的容错与多渠道可用性、以及 GitHub 风格主题色的观感一致性。
- 放大引擎（Real-ESRGAN / Waifu2x / RIFE 等）需在「关于 → 引擎卡」一键下载，或手动放入 `tools/<引擎名>/`。
- 若发现任何问题，欢迎提交 [Issue](https://github.com/GreenChennai/MomentShift/issues) 反馈，附上复现步骤与日志最有效。
