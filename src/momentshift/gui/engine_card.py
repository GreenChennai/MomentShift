"""超分辨率 / 插帧引擎检测卡片（v0.7.5）。

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

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel, QFrame, QWidget, QProgressBar, QSizePolicy

from qfluentwidgets import (
    FluentIcon as FIF, StrongBodyLabel, CaptionLabel, BodyLabel,
    PushButton, HyperlinkButton, PrimaryPushButton,
)

from ..core.qt_compat import QDesktopServices, QUrl, QThreadPool
from ..core import engines as eng_mod
from ..core import engine_download as dl_mod
from ..i18n.translator import tr
from .theme import (
    ThemedCard, CARD_MARGIN, muted_text, accent_color,
    success_color, danger_color, border_color,
)


def open_folder(path: str) -> None:
    """在系统文件管理器里打开目录（Windows 用 explorer，跨平台兜底）。"""
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
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
        f"color: {accent_color().name()}; font-weight: 700; font-size: 10px;"
        f" background: rgba(35,134,54,0.08); border-radius: 3px; padding: 2px 6px;")
    return lbl


class EngineRow(QWidget):
    """单个引擎的检测行。"""

    def __init__(self, engine: eng_mod.Engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.setStyleSheet("background: transparent;")

        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(5)

        # 第一行：状态点 + 名称 + 算法徽标 + 状态文字
        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        top.addWidget(self.dot)
        # v0.7.7 引擎卡布局3：名称与徽标放在子布局中，徽标紧贴名称左对齐
        from .queue_widget import StatusPill as _SP
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self.nameLbl = StrongBodyLabel(engine.name)
        self.nameLbl.setWordWrap(True)
        self.nameLbl.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Preferred)
        name_row.addWidget(self.nameLbl, 1)
        for algo in engine.algos:
            name_row.addWidget(algo_badge(algo, self))
        top.addLayout(name_row, 1)
        # v0.7.7 引擎卡布局2：状态胶囊（对齐转换中 FFmpeg 检测）
        self.statusPill = _SP("pending")
        top.addWidget(self.statusPill)
        vb.addLayout(top)

        # 第二行：说明（v0.7.6 修复2：限宽自动换行，字体随 #515151 提亮）
        self.descLbl = CaptionLabel(tr(engine.desc_key))
        self.descLbl.setWordWrap(True)
        self.descLbl.setMinimumWidth(0)
        self.descLbl.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Preferred)
        self.descLbl.setStyleSheet(
            f"color: {muted_text()}; background: transparent;")
        vb.addWidget(self.descLbl)

        # 第三行：按钮（v0.7.7 引擎卡布局1：左对齐，移到路径提示上方）
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.linkBtn = HyperlinkButton(engine.homepage, tr("engine.goto_download"))
        btns.addWidget(self.linkBtn)
        self.folderBtn = PushButton(tr("engine.open_folder"), icon=FIF.FOLDER)
        self.folderBtn.setFixedHeight(28)
        self.folderBtn.clicked.connect(
            lambda: open_folder(str(eng_mod.engine_dir(self.engine.eid))))
        btns.addWidget(self.folderBtn)
        self.dlBtn = None
        self.reasonLbl = None
        if engine.downloadable:
            self.dlBtn = PrimaryPushButton(tr("engine.download.oneclick"), icon=FIF.DOWNLOAD)
            self.dlBtn.setFixedHeight(28)
            self.dlBtn.clicked.connect(self._one_click)
            btns.addWidget(self.dlBtn)
        else:
            self.reasonLbl = CaptionLabel(tr(engine.download_reason_key))
            self.reasonLbl.setWordWrap(True)
            self.reasonLbl.setStyleSheet(
                f"color: {muted_text()}; background: transparent; font-size: 11px;")
            btns.addWidget(self.reasonLbl)
        btns.addStretch(1)
        vb.addLayout(btns)

        # === 第四行：路径提示（v0.7.7 引擎卡布局1：移到按钮下方）===
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.pathLbl = CaptionLabel(f"tools/{engine.eid}")
        self.pathLbl.setStyleSheet(
            f"color: {muted_text()}; background: transparent; font-size: 11px;")
        path_row.addWidget(self.pathLbl)
        path_row.addStretch(1)
        vb.addLayout(path_row)

        self.prog = QProgressBar()
        self.prog.setRange(0, 0)
        self.prog.setFixedHeight(3)
        self.prog.setTextVisible(False)
        self.prog.setStyleSheet(
            "QProgressBar{border:none;background:transparent;border-radius:1px;}"
            f"QProgressBar::chunk{{background:{accent_color().name()};border-radius:1px;}}")
        self.prog.hide()
        vb.addWidget(self.prog)

        self.refresh()

    # -- 一键下载（v0.7.6：按引擎注册表的下载源，HF→GitHub→官方）--
    def _one_click(self):
        if self.dlBtn:
            self.dlBtn.setEnabled(False)
        self.prog.show()
        worker = dl_mod.EngineDownloadWorker(
            self.engine.eid,
            str(eng_mod.engine_dir(self.engine.eid)),
            list(self.engine.download_sources),
        )
        worker.signals.finished.connect(self._on_dl_done)
        QThreadPool.globalInstance().start(worker)

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
        elif not self.engine.cli:
            color = "#c7920a"
            self.statusPill.set_status("compressing", text=tr("engine.status.driver"))
            self.pathLbl.setText(f"tools/{self.engine.eid}")
        else:
            color = danger_color().name()
            self.statusPill.set_status("failed", text=tr("engine.status.missing"))
            self.pathLbl.setText(f"tools/{self.engine.eid}")
        self.dot.setStyleSheet(f"background:{color}; border-radius:4px;")

    def retranslateUi(self) -> None:
        self.descLbl.setText(tr(self.engine.desc_key))
        self.linkBtn.setText(tr("engine.goto_download"))
        self.folderBtn.setText(tr("engine.open_folder"))
        if self.dlBtn:
            self.dlBtn.setText(tr("engine.download.oneclick"))
        if self.reasonLbl:
            self.reasonLbl.setText(tr(self.engine.download_reason_key))
        self.refresh()


class EnginesCard(ThemedCard):
    """「超分辨率 / 插帧引擎」整卡（关于页专用）。"""

    def __init__(self, parent=None, on_changed=None):
        super().__init__(parent)
        self._on_changed = on_changed
        self._rows: list[EngineRow] = []

        vb = QVBoxLayout(self)
        vb.setContentsMargins(CARD_MARGIN, 16, CARD_MARGIN, 16)
        vb.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.titleLbl = StrongBodyLabel(tr("engine.card.title"))
        head.addWidget(self.titleLbl)
        head.addStretch(1)
        self.rootBtn = PushButton(tr("engine.open_root"), icon=FIF.FOLDER)
        self.rootBtn.setFixedHeight(28)
        self.rootBtn.clicked.connect(self._open_root)
        head.addWidget(self.rootBtn)
        self.rescanBtn = PushButton(tr("engine.rescan"), icon=FIF.SYNC)
        self.rescanBtn.setFixedHeight(28)
        self.rescanBtn.clicked.connect(self.rescan)
        head.addWidget(self.rescanBtn)
        vb.addLayout(head)

        # v0.7.7 引擎卡布局4：确保简介文本自动换行
        self.hintLbl = CaptionLabel(tr("engine.card.hint"))
        self.hintLbl.setWordWrap(True)
        self.hintLbl.setMinimumWidth(0)
        self.hintLbl.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Preferred)
        self.hintLbl.setStyleSheet(
            f"color: {muted_text()}; background: transparent;")
        vb.addWidget(self.hintLbl)

        self.summaryLbl = CaptionLabel("")
        self.summaryLbl.setStyleSheet(
            f"color: {accent_color().name()}; background: transparent; font-weight: 600;")
        vb.addWidget(self.summaryLbl)

        # 两个分组：超分 / 插帧
        self._group_labels = {}
        for cat, key in (("sr", "engine.group.sr"), ("interp", "engine.group.interp")):
            vb.addSpacing(4)
            glbl = CaptionLabel(tr(key))
            glbl.setStyleSheet(
                f"color: {accent_color().name()}; background: transparent;"
                " font-weight: 700; letter-spacing: 0.4px;")
            vb.addWidget(glbl)
            self._group_labels[cat] = (glbl, key)
            for e in eng_mod.ENGINES:
                if e.category != cat:
                    continue
                row = EngineRow(e, self)
                self._rows.append(row)
                vb.addWidget(row)
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setFixedHeight(1)
                sep.setStyleSheet(f"QFrame{{ background: {border_color()}; border: none; }}")
                vb.addWidget(sep)

        eng_mod.ensure_all_dirs()
        self._update_summary()

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
