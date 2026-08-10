"""ffmpeg 状态卡片 + 一键下载（转换界面）。

职责边界：
- 做：展示 ffmpeg 是否就绪，未就绪时提供「一键下载并安装」按钮，并实时显示
  下载进度与失败原因（含可操作的手动部署提示）。
- 不做：不执行下载（交给 FfmpegDownloadWorker）；不探测 ffmpeg 路径。

依赖：core/ffmpeg、core/ffmpeg_download、gui/theme；被依赖：gui/convert_interface。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QVBoxLayout
from qfluentwidgets import BodyLabel, HyperlinkButton, PrimaryPushButton, StrongBodyLabel
from qfluentwidgets import FluentIcon as FIF

from ..core.ffmpeg import ffmpeg_install_dir, find_ffmpeg, get_version
from ..core.ffmpeg_download import FfmpegDownloadWorker
from ..core.qt_compat import QThreadPool, Signal
from ..i18n.translator import tr
from . import tokens
from .theme import ThemedCard, sub_text


class FfmpegCard(ThemedCard):
    ffmpeg_ready = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        vb = QVBoxLayout(self)
        vb.setContentsMargins(16, 14, 16, 14)
        vb.setSpacing(10)

        self.titleLbl = StrongBodyLabel(tr("ffmpeg.title"))
        vb.addWidget(self.titleLbl)

        top = QHBoxLayout()
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        top.addWidget(self.dot)
        self.statusLbl = BodyLabel()
        top.addWidget(self.statusLbl, 1)
        vb.addLayout(top)

        self.linkBtn = HyperlinkButton(
            "https://ffmpeg.org/download.html", tr("ffmpeg.download.page")
        )
        self.dlBtn = PrimaryPushButton(tr("ffmpeg.download"), icon=FIF.DOWNLOAD)
        self.dlBtn.clicked.connect(self._download)
        row = QHBoxLayout()
        row.addWidget(self.linkBtn)
        row.addStretch(1)
        row.addWidget(self.dlBtn)
        vb.addLayout(row)

        self.prog = QProgressBar()
        self.prog.setRange(0, 0)
        self.prog.setFixedHeight(4)
        self.prog.setStyleSheet(
            tokens.progress_qss(tokens.PROGRESS_TRACK, tokens.PROGRESS_CHUNK, 2)
        )
        self.prog.hide()
        vb.addWidget(self.prog)

        # 失败原因 + 手动部署提示（默认隐藏，仅下载失败时显示）。
        self.errLbl = BodyLabel()
        self.errLbl.setObjectName("ffmpegErr")
        self.errLbl.setWordWrap(True)
        self.errLbl.setStyleSheet(
            tokens.text_qss(tokens.DANGER_STRONG, size=tokens.FONT_SMALL, transparent=True)
        )
        self.errLbl.hide()
        vb.addWidget(self.errLbl)

        self._refresh()

    def _refresh(self):
        """按 ffmpeg 是否可用刷新卡片的状态文案、配色与按钮可见性。"""
        path = find_ffmpeg()
        if path:
            ver = get_version(path)
            self.statusLbl.setText(
                tr("ffmpeg.found", name=Path(path).name) + (f"  ·  {ver.split()[0]}" if ver else "")
            )
            text_color, dot_color = tokens.SUCCESS_DOT, tokens.SUCCESS_DOT
            # ffmpeg 已就绪时收起下载与官网按钮，避免「还需要操作」的错觉。
            self.linkBtn.hide()
            self.dlBtn.hide()
            self.prog.hide()
            self.errLbl.hide()
        else:
            self.statusLbl.setText(tr("ffmpeg.missing"))
            text_color, dot_color = sub_text(), tokens.DANGER_DOT
            self.linkBtn.show()
            self.dlBtn.show()
        self.statusLbl.setStyleSheet(f"color:{text_color};")
        self.dot.setStyleSheet(tokens.dot_qss(dot_color))

    def _download(self):
        self.dlBtn.setEnabled(False)
        self.errLbl.hide()
        # 进入下载态：先显示不确定的进度条（总大小未知时也能转），文案提示进行中。
        self.prog.setRange(0, 0)
        self.prog.show()
        self.statusLbl.setText(tr("ffmpeg.downloading"))
        self.statusLbl.setStyleSheet(f"color:{sub_text()};")
        worker = FfmpegDownloadWorker(str(ffmpeg_install_dir()))
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        QThreadPool.globalInstance().start(worker)

    def _on_progress(self, downloaded: int, total: int):
        """实时更新进度条与百分比文案。``total`` 为 0 时退化为不确定进度。"""
        if total and total > 0:
            self.prog.setRange(0, total)
            self.prog.setValue(downloaded)
            pct = downloaded * 100 // total
            self.statusLbl.setText(f"{tr('ffmpeg.downloading')}  {pct}%")
        else:
            if self.prog.maximum() != 0:
                self.prog.setRange(0, 0)
            self.statusLbl.setText(tr("ffmpeg.downloading"))

    def _on_finished(self, ok: bool, msg: str):
        self.prog.hide()
        self.dlBtn.setEnabled(True)
        if ok:
            self.errLbl.hide()
            self._refresh()
            self.ffmpeg_ready.emit()
        else:
            self._refresh()
            self.statusLbl.setText(tr("ffmpeg.missing"))
            self.statusLbl.setStyleSheet(f"color:{tokens.DANGER_STRONG};")
            self.errLbl.setText(f"{tr('ffmpeg.failed_detail', msg=msg)}\n{tr('ffmpeg.manual_hint')}")
            self.errLbl.show()

    def retranslateUi(self):
        self.titleLbl.setText(tr("ffmpeg.title"))
        self.linkBtn.setText(tr("ffmpeg.download.page"))
        self.dlBtn.setText(tr("ffmpeg.download"))
        self._refresh()
