"""MomentShift 视觉令牌 —— 全应用唯一的颜色 / 度量常量来源。

职责边界：
- 做：以纯数据形式定义品牌色、语义状态色、中性色、边框、度量与字号；
  并提供少量把令牌拼成 QSS 片段的构建函数。
- 不做：不定义任何控件类；不 import PyQt6（保持零 Qt 依赖，便于纯 Python 单测）；
  不做主题切换判断（那是 gui/theme.py 的职责）。

依赖：无（本模块是依赖链最底层）；被依赖：gui/theme 及全部 GUI 模块。

为什么独立成模块：改造前全项目散落 99 处 ``#RRGGBB`` 字面量，主色想改一处得全局搜；
收敛到此处后「改一个常量 → 全局生效」，也让 CI 能用一条 grep 守住"别处不许再写死颜色"。

Notes:
    令牌值最初是从原有硬编码色 1:1 抄录的（包括十六进制大小写），因为 B1 的铁律是
    「只换写法，不换颜色」。B1x 已按裁决收敛掉「同色不同写法」的历史遗留
    （``WHITE``/``WARNING`` 各自合并成一个令牌）。

    合并原则（团队裁决，改本模块前请先读）：
    **色值相同且用途相同 → 合并成一个令牌；色值相同但语义独立 → 各留各的令牌。**
    后者的典型是 ``TEXT_SUBTLE`` 与 ``COMPARE_BORDER``（都是 ``#333``），
    语义八竿子打不着，合并会导致将来调其中一个误伤另一个 —— 已就地标注禁止合并。
    近似但不同值的色（绿系、红系）一律**保留语义独立性**，不做统一，
    改为逐条注明用途，让映射表自解释。
"""

from __future__ import annotations


def _rgba(hex_color: str, alpha: str) -> str:
    """把 ``#RRGGBB`` 与定长小数透明度拼成 ``rgba(r,g,b,a)`` 字面量。

    Args:
        hex_color: 六位十六进制色，形如 ``#238636``。
        alpha: 透明度**字符串**，形如 ``"0.08"``。

    Returns:
        形如 ``rgba(35,134,54,0.08)`` 的 QSS 颜色字面量。

    Notes:
        alpha 刻意收字符串而不是 float：这些字符串会逐字进入 QSS，并被
        ``tests/qss_snapshot.py`` 逐字节比对。用字符串把小数位数钉死，
        避免 ``f"{0.15}"`` 这类浮点格式化在不同环境下产出别的表示，
        保证「改成派生」这件事本身不引入任何视觉差异。
    """
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{alpha})"


# ===========================================================================
# 品牌色
# ===========================================================================
ACCENT = "#238636"  # 主色，GitHub 绿（与 setThemeColor 一致）
ACCENT_HOVER = "#2ea043"  # 主色悬停
ACCENT_PRESS = "#196c2e"  # 主色按下

# 主色浅底（同一绿的不同透明度，用于选中态/淡底徽标）。
# 已改为从 ACCENT 派生：改主色即整组联动，不会再出现「主色换了、淡底还是旧绿」。
# 派生结果与改造前的写死字面量逐字符一致，故快照零差异。
ACCENT_SOFT_FAINT = _rgba(ACCENT, "0.04")  # 待处理列表偶数行斑马纹底
ACCENT_SOFT = _rgba(ACCENT, "0.08")  # 后缀/算法名徽标底
ACCENT_SOFT_STRONG = _rgba(ACCENT, "0.15")  # 选中态底

# 主色淡彩组（「转换设置」弹窗的格式大按钮专用）。
# 刻意**不**从 ACCENT 派生：这三个取自 Material Green 色板（50/100/800），
# 与 #238636 不构成任何可计算关系，硬凑派生只会改变色值、破坏视觉零回归。
ACCENT_TINT_BG = "#e8f5e9"  # 淡绿底
ACCENT_TINT_BORDER = "#c8e6c9"  # 淡绿边框 / 悬停底
ACCENT_TINT_TEXT = "#2e7d32"  # 淡绿底上的深绿文字

# ===========================================================================
# 语义状态色
# ===========================================================================
# 绿系 —— 值各不相同，语义与所处视觉层级也不同，**刻意不统一**。
SUCCESS = "#3EB68F"  # 成功/已完成：队列状态胶囊底、尺寸变小的百分比
SUCCESS_DOT = "#10893e"  # 「已就绪」指示点与其文字（仅 ffmpeg 卡片）

# 红系 —— 同上，四个值分属四种视觉层级，**刻意不统一**。
DANGER = "#FF7279"  # 失败：队列状态胶囊底、尺寸变大的百分比
DANGER_TEXT = "#B4324B"  # 危险语义的正文色（比 DANGER 深，正文对比度达标）
DANGER_STRONG = "#d32f2f"  # 错误提示条文字（Material Red 700）
DANGER_DOT = "#e81123"  # 「缺失」指示点（仅 ffmpeg 卡片，与 SUCCESS_DOT 配对）
DANGER_SOFT = "rgba(211,47,47,0.06)"  # 错误提示条底（= DANGER_STRONG 的 6% 透明度）
DANGER_SOFT_STRONG = "rgba(211,47,47,0.12)"  # 错误提示条边框（= DANGER_STRONG 的 12%）

WARNING = "#C7920A"  # 处理中/警示：队列胶囊底、次要动作按钮、引擎卡片状态字

INFO = "#3964FE"  # 压缩完成 / 同格式直通
RUNNING = "#2F98FF"  # 转换中
PENDING = "#8A8A8A"  # 等待中 / 已取消
PROGRESS_CHUNK = "#0f6cbd"  # 下载进度条填充（ffmpeg 卡片）

# ===========================================================================
# 中性色 —— 文字
# ===========================================================================
TEXT_BLACK = "#000000"  # 队列明细等需要最高对比度的正文
TEXT_STRONG = "#212121"  # 主文字
TEXT_TITLE = "#1a1a1a"  # 弹窗大标题
TEXT_BODY = "#424242"  # 帮助弹窗正文
TEXT_SECONDARY = "#757575"  # 次要文字
TEXT_MUTED = "#515151"  # 弱化文字（由过灰的 BORDER_HOVER 调深）
TEXT_PLACEHOLDER = "#9E9E9E"  # 占位符
TEXT_LINK = "#2270F4"  # 链接蓝
# 值与 COMPARE_BORDER 相同（#333），但语义独立：这里是浅色主题下的列表文件名文字，
# 那里是对比窗深色主题下的分隔线。**禁止合并** —— 合并后调其中一个必然误伤另一个。
TEXT_SUBTLE = "#333"  # 列表项文件名、快速弹窗的分栏小标题
ICON_MUTED = "#888888"  # 帮助图标灰

# ===========================================================================
# 中性色 —— 表面与边框
# ===========================================================================
WHITE = "#FFFFFF"  # 窗口底 / 深色按钮上的前景文字 / 输入框底 / 弹窗底

# 表面三态构成一条等距灰阶：常态 245 -> 悬停 238 -> 按下 231，每级 -7。
# SURFACE_PRESS 原本与 SURFACE_HOVER 同为 #EEEEEE，导致「按下去」没有任何视觉反馈，
# 用户无法区分「鼠标划过」与「真的按到了」。改为再降一级，
# 使「按下 vs 悬停」的色差与「悬停 vs 常态」完全等量 —— 既然后者是被接受的反馈强度，
# 前者就不会不可察觉，也不会突兀。
# 下限约束：必须比 BORDER(#E0E0E0, 224) 亮，否则卡片按下时底色会吞掉自己的边框。
SURFACE = "#F5F5F5"  # 卡片/组件表面，亦作深色胶囊上的前景
SURFACE_HOVER = "#EEEEEE"  # 表面悬停
SURFACE_PRESS = "#E7E7E7"  # 表面按下（比悬停再深一级）

BORDER = "#E0E0E0"  # 常规边框 / 分隔线
BORDER_HOVER = "#BDBDBD"  # 边框悬停
INPUT_BORDER = "#d0d0d0"  # 输入类控件边框
PROGRESS_TRACK = "#dcdcdc"  # 进度条轨道

# ===========================================================================
# 对比窗专用深色（独立于全局浅色主题）
# ===========================================================================
COMPARE_BG = "#0d0d0d"  # 画布底
COMPARE_SURFACE = "#1e1e1e"  # 标题栏 / 窗口底
# 值与 TEXT_SUBTLE 相同（#333），但语义独立：见 TEXT_SUBTLE 处的说明。**禁止合并**。
COMPARE_BORDER = "#333"  # 对比窗标题栏分隔线与按钮底
COMPARE_BTN_HOVER = "#444"  # 按钮悬停
COMPARE_TEXT = "#ccc"  # 深色底上的文字

# ===========================================================================
# 滚动条
# ===========================================================================
SCROLLBAR_HANDLE = "rgba(140, 140, 140, 0.6)"
SCROLLBAR_HANDLE_HOVER = "rgba(120, 120, 120, 0.85)"

# ===========================================================================
# 度量
# ===========================================================================
RADIUS_XS, RADIUS_SM, RADIUS_MD, RADIUS_LG = 3, 4, 8, 12
RADIUS = 12  # 卡片默认圆角
SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG = 4, 8, 12, 16
SPACING = 12  # 布局默认间距
CARD_MARGIN = 16  # 卡片内边距

# ===========================================================================
# 字号
# ===========================================================================
FONT_MICRO = 10
FONT_CAPTION = 11
FONT_SMALL = 12
FONT_BODY = 13
FONT_SUBTITLE = 15
FONT_LARGE = 16
FONT_DIALOG_TITLE = 17
FONT_TITLE = 18


# ===========================================================================
# QSS 构建器
# ===========================================================================
def text_qss(
    color: str,
    *,
    size: int | None = None,
    weight: int | None = None,
    transparent: bool = False,
    extra: str = "",
) -> str:
    """拼出一段纯文字样式。

    Args:
        color: 文字颜色令牌。
        size: 字号（px）；``None`` 表示不指定。
        weight: 字重；``None`` 表示不指定。
        transparent: 是否追加透明背景。
        extra: 追加在末尾的原样 QSS 声明（如 ``"padding: 24px 0;"``）。

    Returns:
        形如 ``color: #000000; background: transparent;`` 的 QSS 片段。

    Notes:
        **QLabel 只要设了 ``color:`` 就必须一并给透明背景**，否则 Qt 会给标签
        铺一层默认底色，表现为「文字下方一块灰」——项目历史上反复踩过。
        因此凡是给 QLabel 用的调用都应传 ``transparent=True``。

        本函数固定按 ``color → font-size → font-weight → background → extra``
        的顺序输出。改造前若干处的书写顺序与此不同（例如
        ``color; background; font-size``），归一化后属于「仅顺序差异」，
        QSS 层叠语义不变。
    """
    parts = [f"color: {color};"]
    if size is not None:
        parts.append(f"font-size: {size}px;")
    if weight is not None:
        parts.append(f"font-weight: {weight};")
    if transparent:
        parts.append("background: transparent;")
    if extra:
        parts.append(extra)
    return " ".join(parts)


def dot_qss(color: str, radius: int = 5) -> str:
    """圆形状态指示点。"""
    return f"background:{color}; border-radius:{radius}px;"


def pill_qss(
    fg: str,
    bg: str,
    radius: int = 9,
    *,
    padding: str = "2px 9px",
    weight: int = 600,
    size: int | None = None,
) -> str:
    """胶囊标签（队列状态胶囊、格式指示胶囊）。

    Args:
        fg: 前景文字色。
        bg: 胶囊底色。
        radius: 圆角半径。
        padding: 内边距。
        weight: 字重。
        size: 字号（px）；``None`` 表示沿用控件默认字号。

    Returns:
        胶囊 QSS 片段。声明顺序刻意与改造前的历史写法保持一致，
        使 QSS 快照能逐字节对齐。
    """
    qss = (
        f"color:{fg}; background:{bg}; border-radius:{radius}px;"
        f" padding:{padding}; font-weight:{weight};"
    )
    if size is not None:
        qss += f" font-size:{size}px;"
    return qss


def input_qss(selector: str, radius: int) -> str:
    """输入类控件（QLineEdit / QSpinBox）的统一外观。

    Args:
        selector: QSS 选择器，如 ``QLineEdit``。
        radius: 圆角半径。
    """
    return (
        f"{selector} {{ border: 1px solid {INPUT_BORDER}; border-radius: {radius}px;"
        f" padding: 4px 8px; background: {WHITE}; }}"
    )


def accent_button_qss(
    radius: int = RADIUS_MD, *, padding: str = "8px 20px", size: int = FONT_BODY, weight: int = 600
) -> str:
    """主色实心按钮（含 hover / pressed 三态）。"""
    return (
        f"QPushButton{{ background: {ACCENT}; color: {WHITE}; border: none;"
        f" border-radius: {radius}px; padding: {padding};"
        f" font-size: {size}px; font-weight: {weight}; }}"
        f"QPushButton:hover{{ background: {ACCENT_HOVER}; }}"
        f"QPushButton:pressed{{ background: {ACCENT_PRESS}; }}"
    )


def warning_button_qss(
    radius: int = 6, *, padding: str = "0 24px", size: int | None = None, weight: int = 600
) -> str:
    """警示色实心按钮（「载入中…」这类需要提醒但不可点的过渡态）。

    Args:
        radius: 圆角半径。
        padding: 内边距。
        size: 字号（px）；``None`` 表示沿用按钮默认字号。
        weight: 字重。

    Notes:
        默认参数刻意对齐改造前 ``set_loading`` 里那两处逐字相同的内联样式，
        使快照可逐字节对齐。
    """
    qss = (
        f"QPushButton{{background:{WARNING};color:{WHITE};border:none;"
        f"border-radius:{radius}px;padding:{padding};"
    )
    if size is not None:
        qss += f"font-size:{size}px;"
    return qss + f"font-weight:{weight};}}"


def status_hint_qss(
    fg: str,
    bg: str,
    border: str,
    *,
    radius: int = 6,
    size: int = FONT_SMALL,
    weight: int = 600,
    padding: str = "2px 10px",
) -> str:
    """状态提示胶囊（文字 + 淡底 + 1px 同色系描边），用于 ffmpeg 就绪/缺失提示。"""
    return (
        f"QLabel{{ color:{fg}; font-weight:{weight}; font-size:{size}px;"
        f" background:{bg}; border:1px solid {border};"
        f" border-radius: {radius}px; padding: {padding}; }}"
    )


def ext_badge_qss(
    color: str = ACCENT, *, size: int = FONT_CAPTION, padding: str = "1px 4px"
) -> str:
    """文件后缀 / 算法名徽标：品牌绿淡底 + 小圆角 + 粗体短文字。"""
    return (
        f"color: {color}; font-weight: 700; font-size: {size}px;"
        f" background: {ACCENT_SOFT}; border-radius: {RADIUS_XS}px;"
        f" padding: {padding};"
    )


def staging_row_qss(tinted: bool) -> str:
    """「待处理文件」列表的斑马纹行底。

    Args:
        tinted: ``True`` 用主色极淡底（偶数行），``False`` 用透明底（奇数行）。
    """
    bg = ACCENT_SOFT_FAINT if tinted else "transparent"
    return f"background: {bg}; border-radius: {RADIUS_SM}px;"


def scrollarea_qss(radius: int | None = None) -> str:
    """QScrollArea 的统一外观：无边框 + 透明底 + 全局细滚动条。

    Args:
        radius: 圆角半径；``None`` 表示不设圆角。
    """
    extra = f" border-radius: {radius}px;" if radius is not None else ""
    return f"QScrollArea{{ border: none; background: transparent;{extra} }} {scrollbar_qss()}"


def progress_qss(track: str, chunk: str, radius: int) -> str:
    """细进度条（轨道 + 填充）。"""
    return (
        f"QProgressBar{{background:{track}; border:none; border-radius:{radius}px;}} "
        f"QProgressBar::chunk{{background:{chunk}; border-radius:{radius}px;}}"
    )


def scrollbar_qss() -> str:
    """全局细滚动条样式（纵向 + 横向）。"""
    handle, hover = SCROLLBAR_HANDLE, SCROLLBAR_HANDLE_HOVER
    return (
        "QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }"
        "QScrollBar::handle:vertical {"
        f" background: {handle}; border-radius: 4px; min-height: 30px;"
        " }"
        f"QScrollBar::handle:vertical:hover {{ background: {hover}; }}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
        "QScrollBar:horizontal { background: transparent; height: 8px; margin: 0; }"
        "QScrollBar::handle:horizontal {"
        f" background: {handle}; border-radius: 4px; min-width: 30px;"
        " }"
        f"QScrollBar::handle:horizontal:hover {{ background: {hover}; }}"
        "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }"
        "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal"
        " { background: transparent; }"
    )


def transparent_children_qss(*selectors: str) -> str:
    """把若干选择器的背景统一置为透明。

    Notes:
        必须用 ``#objectName`` 或类名限定；裸写 ``background-color: transparent``
        会级联到所有子控件，是项目里「文字底部灰色色块」的历史成因。
    """
    return "".join(f"{sel} {{ background-color: transparent; }}" for sel in selectors)
