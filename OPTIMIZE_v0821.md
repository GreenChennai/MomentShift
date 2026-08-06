# V0.8.21 优化清单

> 流程：逐项讨论 → 确认 → 实施 → 验证 → 单项提交。新发现正式入清单编号。
> 本版本**不推 GitHub**，仅本地构建。

---

## A. 版本与交付

| 编号 | 项 | 位置 | 状态 |
|---|---|---|---|
| A1 | 版本号 0.8.20 → 0.8.21 | `metadata.py:15`、`pyproject.toml:8`、`README.md:10` | 待办 |
| A2 | README 版本信息与更新说明 | `README.md` | 待办 |
| A3 | 本地构建 Windows 版 → `E:/平日资料/构建/MomentShift-v0.8.21/` | `build.spec` | 待办 |

---

## B. Bug 修复

### B1 · FFmpeg 压缩视频选 nvenc 必失败（AMD 9070 GRE）

**现象**：压缩预设「最小体积」+ 视频编码器 `h264_nvenc` / `hevc_nvenc` →
`Could not open encoder before EOF` / `Error while opening encoder`。

**根因**（三层全断）：

1. **UI 层无门禁** — `ffmpeg_compress.FFMPEG_VIDEO_PARAMS["ff_v_encoder"].values`
   静态写死含 `h264_nvenc`/`hevc_nvenc`，`compress_task_panel._build_ff_param`
   原样渲染成下拉项，不问显卡。用户是 AMD 9070 GRE，NVIDIA 专属编码器永远打不开。
2. **核心层无校验** — `ffmpeg_compress.pick_video_encoder()` 只按容器选，
   不调用任何可用性检查。
3. **兜底层不覆盖** — `ffmpeg_compress.run()` 的 safe-retry 只识别「选项无效」，
   不识别「编码器打不开」，所以没能降级 CPU。

**讽刺点**：`core/hardware.py` 里 `encoder_usable()` / `detect_hw_accel()` /
`_probe_encoder()`（真跑一帧 `nullsrc` 试编码）**早就写好了**，转换链路
（`converter.py`）也在用，唯独压缩链路一次都没调。

**修复方案**：

- B1-a UI 门禁：构建 `ff_v_encoder` 下拉时查 `detect_hw_accel()`，
  不可用项 `setItemEnabled(idx, False)`，标签追加「(本机不可用)」。
  探测异步跑，结果走 `hardware._PROBE_CACHE`，不卡对话框弹出。
- B1-b 核心校验：`pick_video_encoder()` 加 `encoder_usable()` 守卫，
  请求硬件编码器但不可用 → 自动回落 `libx264` / `libx265`。
- B1-c 兜底重试：`run()` 的 retry 识别 `Could not open encoder` /
  `Error while opening encoder`，剥掉硬件编码器 + `-rc/-cq` 换 CPU 参数重试一次。

### B2 · 「文件名后缀」标签残留

**现象**：`创建压缩任务-音频/视频/图片` 里输出位置从「保存在源文件旁」切到
「指定文件夹」，多出一个居中的无意义字符串「文件名后缀」。

**根因**：`compress_task_panel.py:367 _apply_output_mode()`

```python
self.suffixEdit.setVisible(same)     # 只隐藏了输入框
self._folderRow.setVisible(not same) # 文件夹行是整行隐藏，对的
```

后缀是用 `field_row(tr("compress.output.suffix"), self.suffixEdit)` 建的
**一整行（标签 + 输入框）**，只隐藏输入框 → 标签独自留在行里，
被布局居中显示，就是那个「无意义字符串」。

**修复**：持有 `self._suffixRow` 引用，整行隐藏，与 `_folderRow` 对称。

---

## C. 功能调整：压缩设置同步进转换高级设置

### C1 · 转换设置-视频 → 高级设置

- 保留：`合并为单个文件`、`仅提取音频`、`音频格式`
- 删除：分辨率、帧率、码率、编码器、CRF（现 `_add_video` 其余全部）
- 新增：`FFMPEG_VIDEO_PARAMS` 全部 11 项（profile / encoder / crf / preset /
  tune / pixfmt / maxwidth / fps / audio / abr / faststart）
- **不含** 输出位置、文件名后缀、输出文件夹

### C2 · 转换设置-音频 → 高级设置

- 保留：`合并为单个文件`
- 删除：码率、采样率、声道
- 新增：`FFMPEG_AUDIO_PARAMS` 全部 7 项（profile / encoder / bitrate / mp3q /
  flac_level / channels / samplerate）
- **不含** 输出位置、文件名后缀、输出文件夹

**连带改动**：`core/advanced.py` 的 `default_options()` 与
`build_advanced_args()` 需要重建 video / audio 两个分类的键。

**⚠ 待你拍板的设计分歧**（见下方「需确认」）：转换时这些压缩参数怎么落地。

---

## D. 真进度条（核心）

### D1 · 修 `duration_ms` 键错误（同一 bug 两处）

`ffmpeg_compress.py:1070` 与 `converter.py:165` 都写着：

```python
if key == "duration_ms" and val.isdigit():
    duration_ms = int(val)
elif key == "out_time_ms" and val.isdigit() and duration_ms:  # 永远进不来
    pct = ...
```

ffmpeg 的 `-progress` **从不输出 `duration_ms`**，所以分母恒为 `None`，
中间进度一次都不发，只有 `progress=end` 时发一个 100。这就是「假进度条」的由来。

且 ffmpeg 的 `out_time_ms` 单位实际是**微秒**（历史 bug），直接当毫秒用会差 1000 倍。

**修复**：改用 `out_time`（`HH:MM:SS.ffffff` 字符串，语义无歧义）为主，
`out_time_us` 为辅。

### D2 · 分母来源

`-progress` 不给总时长，必须外部取。两级：

1. ffprobe 预取（`-v error -show_entries format=duration`），复用
   `advanced.probe_video_size()` 的调用模式，约 30ms
2. 兜底：从合流输出里正则抓 ffmpeg 自己打的 `Duration: HH:MM:SS.ss`
   （`-hide_banner` 不影响这一行）

### D3 · 速度 / ETA / 已编码时长

解析 `speed=1.53x`，`ETA = (总时长 - 已编码) / speed`。
FFmpegFreeUI 就是这么算的。

### D4 · UI 节流

FFmpegFreeUI 用 1s 源节流 + 500ms 合并定时器 + 单行刷新。
本项目 `FakeProgressDriver` 已是 500ms QTimer，直接复用为节流器，
不新增定时器。

### D5 · 假进度条降级为兜底

保留 `max(fake, real)` 合并策略。真进度一旦到达立即接管；
仅在图片压缩（无时长概念）等真进度不可得的场景继续用假进度。

---

## E. 吸收 FFmpegFreeUI 可取之处

| 编号 | 项 | 说明 |
|---|---|---|
| E1 | 队列行展示 速度 / ETA / 已处理时长 | 对应 D3 的数据落到 UI |
| E2 | 日志内存管理 | 超限时**只丢进度行**，保留错误行，避免长任务日志爆内存 |
| E3 | epoch token 防竞态 | 异步回调带世代号，任务重启后旧回调自动作废 |
| E4 | psutil 暂停/恢复 | 需新增 `psutil` 依赖，**建议本版跳过**，另议 |

---

## 需你确认的两点

### Q1 · C1/C2 落地方式：一步转码 vs 两段式

转换链路（`converter.py`）和压缩链路（`ffmpeg_compress.py`）是两套独立的
命令构造器。压缩参数进了「转换高级设置」后：

- **方案甲（推荐）**：转换命令直接采用 `ffmpeg_compress` 的参数构造，
  转换 = 一次带压缩参数的转码。**一步到位，无中间文件，快约一倍，画质只损一次**。
- **方案乙**：沿用现有两段式（转换 → 压缩，队列已有 `COMPRESSING` 黄→蓝状态机）。
  改动小，但要落一个中间文件、编码两次、画质损两遍。

### Q2 · 提交粒度

按 product-optimize 的规矩是「一项一提交」。但你之前版本的习惯是合并提交。
本版本按哪种来？
