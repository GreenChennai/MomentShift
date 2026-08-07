"""超分辨率 / 插帧引擎检测卡片。

职责边界：
- 做：展示单个引擎的就绪状态、提供下载按钮与进度、下载完成后刷新状态。
- 不做：不实现下载与解压（交给 core/engine_download）。

依赖：core/config、core/engine_download、core/engines、core/qt_compat、gui/queue_widget、gui/theme、i18n/translator；被依赖：gui/about_interface。

引擎不随软件分发。本卡片负责：
- 逐条检测 ``tools/<engine-id>/`` 下的引擎是否就位
- 展示每个引擎实现的算法与用途说明
- 「前往下载」跳官网、「打开文件夹」直达该引擎的存放目录
- Real-ESRGAN 额外保留一键下载（官方 release 自带模型，可直接装好）

放在「关于」页，与 FFmpeg 卡片分开。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QPropertyAnimation, Qt
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    HyperlinkButton,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from ..core import engine_download as dl_mod
from ..core import engines as eng_mod
from ..core.qt_compat import QDesktopServices, QThreadPool, QUrl
from ..i18n.translator import tr
from . import animations, tokens
from .theme import (
    ArrowToggle,
    CARD_MARGIN,
    ThemedCard,
    accent_color,
    apply_text,
    apply_transparent,
    border_color,
    danger_color,
    muted_text,
    success_color,
)


def open_folder(path: str) -> None:
    """在系统文件管理器里打开目录（Windows 用 explorer，跨平台兜底）。"""
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:  # 静默原因：引擎目录创建失败非致命，后续下载会再次报错
        pass
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # noqa: S606 - 打开资源管理器
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except OSError:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))


def algo_badge(text: str, parent=None) -> QLabel:
    """算法名徽标（与 ext_badge 同一视觉语言）。"""
    lbl = QLabel(text, parent)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(
        tokens.ext_badge_qss(accent_color().name(), size=tokens.FONT_MICRO, padding="2px 6px")
    )
    return lbl


class EngineRow(QWidget):
    """单个引擎的检测行。"""

    def __init__(self, engine: eng_mod.Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        apply_transparent(self)

        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(5)

        # 第一行：状态点 + 名称 + 算法徽标 + 状态文字
        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        top.addWidget(self.dot)
        # 引擎卡布局3：名称与徽标放在子布局中，徽标紧贴名称左对齐
        from .queue_widget import StatusPill as _SP

        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self.nameLbl = StrongBodyLabel(engine.name)
        self.nameLbl.setWordWrap(True)
        self.nameLbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        name_row.addWidget(self.nameLbl, 1)
        for algo in engine.algos:
            name_row.addWidget(algo_badge(algo, self))
        top.addLayout(name_row, 1)
        # 引擎卡布局2：状态胶囊（对齐转换中 FFmpeg 检测）
        self.statusPill = _SP("pending")
        top.addWidget(self.statusPill)
        vb.addLayout(top)

        # 第二行：说明（ 修复2：限宽自动换行，字体随 #515151 提亮）
        self.descLbl = CaptionLabel(tr(engine.desc_key))
        self.descLbl.setWordWrap(True)
        self.descLbl.setMinimumWidth(0)
        self.descLbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        apply_text(self.descLbl, muted_text(), transparent=True)
        vb.addWidget(self.descLbl)

        # 第三行：按钮（ 引擎卡布局1：左对齐，移到路径提示上方）
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.linkBtn = HyperlinkButton(engine.homepage, tr("engine.goto_download"))
        btns.addWidget(self.linkBtn)
        self.folderBtn = PushButton(tr("engine.open_folder"), icon=FIF.FOLDER)
        self.folderBtn.setFixedHeight(28)
        self.folderBtn.clicked.connect(
            lambda: open_folder(str(eng_mod.engine_dir(self.engine.eid)))
        )
        btns.addWidget(self.folderBtn)
        self.dlBtn = None
        self.reasonLbl = None
        # 一键下载按钮仅在「本平台有可用下载源」时显示；否则（如 Windows-only 引擎
        # 在 Linux/macOS 上）显示原因说明，引导用户手动前往官网下载。
        if engine.downloadable and eng_mod.download_sources_for(engine.eid):
            self.dlBtn = PrimaryPushButton(tr("engine.download.oneclick"), icon=FIF.DOWNLOAD)
            self.dlBtn.setFixedHeight(28)
            self.dlBtn.clicked.connect(self._one_click)
            btns.addWidget(self.dlBtn)
        else:
            reason_key = engine.download_reason_key or "engine.reason.platform"
            self.reasonLbl = CaptionLabel(tr(reason_key))
            self.reasonLbl.setWordWrap(True)
            apply_text(self.reasonLbl, muted_text(), size=tokens.FONT_CAPTION, transparent=True)
            btns.addWidget(self.reasonLbl)
        btns.addStretch(1)
        vb.addLayout(btns)

        # === 第四行：路径提示（ 引擎卡布局1：移到按钮下方）===
        # 路径可能很长，开启自动换行（同简介），防止顶出卡片
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.pathLbl = CaptionLabel(f"tools/{engine.eid}")
        self.pathLbl.setWordWrap(True)
        self.pathLbl.setMinimumWidth(0)
        self.pathLbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        apply_text(self.pathLbl, muted_text(), size=tokens.FONT_CAPTION, transparent=True)
        path_row.addWidget(self.pathLbl)
        path_row.addStretch(1)
        vb.addLayout(path_row)

        self.prog = QProgressBar()
        # v0.8.14 软件功能调整：与「音频转文字」模型管理同步，下载时显示百分比胶囊，
        # 故进度条设为 0..100 确定性范围（而非 0,0 不确定动画）。
        self.prog.setRange(0, 100)
        self.prog.setFixedHeight(3)
        self.prog.setTextVisible(False)
        self.prog.setStyleSheet(tokens.progress_qss("transparent", accent_color().name(), 1))
        self.prog.hide()
        vb.addWidget(self.prog)

        # V0.8.20 动画2：行悬停高亮（浅灰底 + 圆角，objectName 限定不影响子控件）
        self.setObjectName("engineRow")
        self.refresh()

    def enterEvent(self, event):
        self._set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_hover(False)
        super().leaveEvent(event)

    def _set_hover(self, hover: bool):
        """切换行悬停底色。对象选择器只作用于本行，不级联到子标签。"""
        try:
            if hover:
                self.setStyleSheet(
                    f"#engineRow{{ background: {tokens.SURFACE_HOVER}; border-radius: 6px; }}"
                )
            else:
                self.setStyleSheet("background: transparent;")
        except RuntimeError:
            pass  # 静默原因：控件可能已随界面销毁

    # -- 一键下载：按引擎注册表中「当前平台」的下载源，HF→GitHub→官方 --
    def _one_click(self):
        sources = eng_mod.download_sources_for(self.engine.eid)
        if not sources:
            return
        if self.dlBtn:
            self.dlBtn.setEnabled(False)
        self.prog.setValue(0)
        self.prog.show()
        worker = dl_mod.EngineDownloadWorker(
            self.engine.eid,
            str(eng_mod.engine_dir(self.engine.eid)),
            sources,
        )
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_dl_done)
        QThreadPool.globalInstance().start(worker)

    def _on_progress(self, _eid: str, pct: int):
        """v0.8.14：下载进度 → 进度条 + 状态胶囊显示「下载中 x%」。"""
        self.prog.setValue(pct)
        self.statusPill.set_status("compressing", text=tr("engine.status.downloading", pct=pct))

    def _on_dl_done(self, ok: bool, msg: str):
        if self.dlBtn:
            self.dlBtn.setEnabled(True)
        self.prog.hide()
        self.refresh()
        parent = self.parent()
        while parent is not None and not hasattr(parent, "engineChanged"):
            parent = parent.parent()
        if parent is not None:
            parent.engineChanged()

    # -- 状态刷新 --
    def refresh(self) -> None:
        exe = eng_mod.find_engine(self.engine.eid)
        if exe:
            color = success_color().name()
            self.statusPill.set_status("done", text=tr("engine.status.ready"))
            self.pathLbl.setText(str(Path(exe).parent))
            # 模型已可用 → 隐藏一键下载，防止重复下载
            if self.dlBtn is not None:
                self.dlBtn.hide()
        elif not self.engine.cli:
            color = tokens.WARNING
            self.statusPill.set_status("compressing", text=tr("engine.status.driver"))
            self.pathLbl.setText(f"tools/{self.engine.eid}")
            if self.dlBtn is not None:
                self.dlBtn.show()
        else:
            color = danger_color().name()
            self.statusPill.set_status("failed", text=tr("engine.status.missing"))
            self.pathLbl.setText(f"tools/{self.engine.eid}")
            if self.dlBtn is not None:
                self.dlBtn.show()
        self.dot.setStyleSheet(tokens.dot_qss(color, 4))

    def retranslateUi(self) -> None:
        self.descLbl.setText(tr(self.engine.desc_key))
        self.linkBtn.setText(tr("engine.goto_download"))
        self.folderBtn.setText(tr("engine.open_folder"))
        if self.dlBtn:
            self.dlBtn.setText(tr("engine.download.oneclick"))
        if self.reasonLbl:
            # V0.8.19 优化9：与构造路径的 or 兜底保持一致，
            # 避免 download_reason_key 为空串时 tr("") 空白
            reason_key = self.engine.download_reason_key or "engine.reason.platform"
            self.reasonLbl.setText(tr(reason_key))
        self.refresh()


class EnginesCard(ThemedCard):
    """「超分辨率 / 插帧引擎」整卡。

    v0.7.8 引擎卡布局：增加展开/收起；按钮居中在简介下方；分组标题加大字号。
    v0.8.23：折叠按钮改为与其他可折叠卡片一致的箭头（``ArrowToggle``），
    补齐展开 / 收起动画（250ms maximumHeight + 箭头旋转）；默认折叠，供
    「放大」界面在「放大设置」上方嵌入。
    """

    _ANIM_DURATION = animations.DURATION_CARD

    def __init__(self, parent=None, on_changed=None, collapsed=True):
        super().__init__(parent)
        self._on_changed = on_changed
        self._rows: list[EngineRow] = []
        self._collapsed = collapsed
        self._anim = None
        self._content_height = 0
        self._bar_h_fixed = None  # 收起动画期间钉死的标题栏高度（同 CollapsibleCard）

        vb = QVBoxLayout(self)
        vb.setContentsMargins(CARD_MARGIN, 16, CARD_MARGIN, 16)
        vb.setSpacing(10)

        # 标题行（仅标题 + 箭头折叠按钮）。包成独立 widget 以便收起动画期间
        # 钉死其高度，防止 body 收缩带动标题上下抖（同 CollapsibleCard 做法）。
        self._bar = QWidget(self)
        apply_transparent(self._bar)
        head = QHBoxLayout(self._bar)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.titleLbl = StrongBodyLabel(tr("engine.card.title"))
        head.addWidget(self.titleLbl)
        head.addStretch(1)
        self.toggleBtn = ArrowToggle(self)
        self.toggleBtn.setFixedSize(30, 30)
        # 初始角度：收起=箭头向下(0°)，展开=箭头向上(180°)
        self.toggleBtn.angle = 0.0 if collapsed else 180.0
        self.toggleBtn.clicked.connect(self._toggle_expand)
        head.addWidget(self.toggleBtn)
        vb.addWidget(self._bar)

        # 简介
        self.hintLbl = CaptionLabel(tr("engine.card.hint"))
        self.hintLbl.setWordWrap(True)
        self.hintLbl.setMinimumWidth(0)
        self.hintLbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        apply_text(self.hintLbl, muted_text(), transparent=True)
        vb.addWidget(self.hintLbl)

        # 引擎卡布局2：按钮居中在简介下方
        btns_center = QHBoxLayout()
        btns_center.addStretch(1)
        self.rootBtn = PushButton(tr("engine.open_root"), icon=FIF.FOLDER)
        self.rootBtn.setFixedHeight(28)
        self.rootBtn.clicked.connect(self._open_root)
        btns_center.addWidget(self.rootBtn)
        btns_center.addSpacing(8)
        self.rescanBtn = PushButton(tr("engine.rescan"), icon=FIF.SYNC)
        self.rescanBtn.setFixedHeight(28)
        self.rescanBtn.clicked.connect(self.rescan)
        btns_center.addWidget(self.rescanBtn)
        btns_center.addStretch(1)
        vb.addLayout(btns_center)

        self.summaryLbl = CaptionLabel("")
        apply_text(self.summaryLbl, accent_color().name(), weight=600, transparent=True)
        vb.addWidget(self.summaryLbl)

        # 引擎卡布局1：可展开/收起的引擎列表区域
        self._body = QWidget()
        apply_transparent(self._body)
        bv = QVBoxLayout(self._body)
        bv.setContentsMargins(0, 0, 0, 0)
        bv.setSpacing(6)

        self._group_labels = {}
        for cat, key in (("sr", "engine.group.sr"), ("interp", "engine.group.interp")):
            bv.addSpacing(4)
            # 引擎卡布局3：分组标题加大字号（StrongBodyLabel 代替 CaptionLabel）
            glbl = StrongBodyLabel(tr(key))
            apply_text(glbl, accent_color().name(), size=tokens.FONT_LARGE, transparent=True)
            bv.addWidget(glbl)
            self._group_labels[cat] = (glbl, key)
            for e in eng_mod.ENGINES:
                if e.category != cat:
                    continue
                row = EngineRow(e, self)
                self._rows.append(row)
                bv.addWidget(row)
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setFixedHeight(1)
                sep.setStyleSheet(f"QFrame{{ background: {border_color()}; border: none; }}")
                bv.addWidget(sep)

        vb.addWidget(self._body)

        eng_mod.ensure_all_dirs()
        self._update_summary()

        if collapsed:
            # 初始化即时折叠（不播动画，避免「先铺开再收拢」闪烁）
            self._body.setMaximumHeight(0)
            self._body.setVisible(False)

    def _toggle_expand(self):
        self.setCollapsed(not self._collapsed)

    def setCollapsed(self, collapsed: bool):
        """展开/收起引擎列表（带动画）。外部（如「检测环境」按钮）也可调用。"""
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        if collapsed:
            self._apply_collapsed()
        else:
            self._apply_expanded()

    def isCollapsed(self) -> bool:
        return self._collapsed

    def _spin_toggle_icon(self) -> None:
        """箭头 180° 平滑旋转（收起=0° 下拉 / 展开=180° 上收）。"""
        animations.animate_value(
            self.toggleBtn,
            b"angle",
            0.0 if self._collapsed else 180.0,
            duration=animations.DURATION_CARD,
            curve=animations.CURVE_SMOOTH,
            animate=animations.should_animate(self.toggleBtn),
        )

    def _anim_target(self, target_h: int):
        if self._anim is not None:
            self._anim.stop()
            self._anim.deleteLater()
            self._anim = None
        cur = self._body.maximumHeight()
        real_target = target_h
        if target_h <= 0:
            real_target = 0
            h = self._body.height()
            if h > 0 and cur > h:
                cur = h
        elif target_h == 16777215:
            if self._content_height > 0:
                real_target = self._content_height
            else:
                real_target = self._body.sizeHint().height()
                if real_target <= 0:
                    real_target = 200
        self._content_height = real_target if real_target > 0 else self._content_height
        self._body.show()
        self._anim = QPropertyAnimation(self._body, b"maximumHeight", self)
        self._anim.setDuration(self._ANIM_DURATION)
        self._anim.setStartValue(cur)
        self._anim.setEndValue(real_target)
        self._anim.setEasingCurve(
            animations.CURVE_OUT if target_h <= 0 else animations.CURVE_IN
        )
        self._anim.finished.connect(self._on_anim_finished)
        self._anim.start()

    def _on_anim_finished(self):
        if self._bar_h_fixed is not None:
            self._bar.setMinimumHeight(0)
            self._bar.setMaximumHeight(16777215)
            self._bar_h_fixed = None
        if self._collapsed:
            self._body.setVisible(False)
        else:
            self._body.setMaximumHeight(16777215)

    def _apply_collapsed(self):
        self._collapsed = True
        h = self._body.height()
        if h > 0:
            self._content_height = h
        # 收起动画期间钉死标题栏高度，避免高度收缩带动标题上下抖
        self._bar_h_fixed = self._bar.height()
        self._bar.setFixedHeight(self._bar_h_fixed)
        self._anim_target(0)
        self._spin_toggle_icon()

    def _apply_expanded(self):
        self._collapsed = False
        self._body.setVisible(True)
        self._anim_target(16777215)
        self._spin_toggle_icon()

    def refresh_content_height(self):
        """内容动态变化后调用：展开态下解除 maximumHeight 上限。"""
        self._content_height = 0
        if not self._collapsed:
            self._body.setMaximumHeight(16777215)

    # 供 EngineRow 冒泡通知
    def engineChanged(self) -> None:
        self._update_summary()
        if callable(self._on_changed):
            self._on_changed()

    def _open_root(self):
        from ..core.config import tools_dir

        open_folder(str(tools_dir()))

    def rescan(self) -> None:
        eng_mod.ensure_all_dirs()
        for row in self._rows:
            row.refresh()
        self.engineChanged()

    def _update_summary(self) -> None:
        sr = len(eng_mod.installed_engines("sr"))
        it = len(eng_mod.installed_engines("interp"))
        self.summaryLbl.setText(tr("engine.card.summary", sr=sr, interp=it))

    def retranslateUi(self) -> None:
        self.titleLbl.setText(tr("engine.card.title"))
        self.hintLbl.setText(tr("engine.card.hint"))
        self.rootBtn.setText(tr("engine.open_root"))
        self.rescanBtn.setText(tr("engine.rescan"))
        for cat, (lbl, key) in self._group_labels.items():
            lbl.setText(tr(key))
        for row in self._rows:
            row.retranslateUi()
        self._update_summary()
