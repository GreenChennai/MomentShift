"""单个转码任务的数据模型。

职责边界：
- 做：用无 Qt 依赖的轻量 dataclass 描述一个待转码文件及其状态。
- 不做：不含任何 UI 逻辑；不触达队列或引擎。

依赖：标准库；被依赖：core/queue 等。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Task:
    """描述一个待转码文件及其全生命周期状态。

    典型用法::

        task = Task(id="ab12", input_path="a.png", output_path="a.jpg",
                    target_format="jpg", category="image", use_gpu=False)

    线程约定：入队后 ``status`` / ``progress`` 等字段会被 worker 线程写、被 GUI
    线程读。字段都是标量或整体替换的容器，配合 Qt 信号做同步，不额外加锁。
    ``status`` 取值见下方常量。
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPRESSING = "compressing"  # 转换完成，正在压缩
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"

    id: str
    input_path: str
    output_path: str
    target_format: str  # 预设键，如 "jpg" / "mp4"
    category: str  # 源分类："image" | "audio" | "video"
    use_gpu: bool

    status: str = PENDING
    progress: int = 0  # 取值 0..100
    error: str = ""
    duration_ms: int = 0
    src_size: int = 0  # 源文件字节数，入队时填充
    dst_size: int = 0  # 输出文件字节数，转换完成后填充

    # 高级转换参数快照（见 core/advanced.py）。为 None 表示走默认值。
    adv: dict = None
    # 用户是否启用了高级设置（控制转换后压缩）
    compress_enabled: bool = False
    compress_progress: int = 0  # 压缩阶段进度 0..100
    compress_done: bool = False  # 压缩完成
    pre_compress_size: int = 0  # 压缩前 dst_size（用于对比）
    # 合并模式：为 True 时用 input_paths（而非单个 input_path）拼成一个输出，
    # 对应高级选项「合并为一个文件」
    merge: bool = False
    input_paths: list = None
