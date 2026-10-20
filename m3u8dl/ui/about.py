"""关于对话框: 版本 / 版权 / 致谢。

v1.1.19: 整体压低高度。原来是一整块 ``line-height:1.8`` 的富文本 + 两处
``<br><br>`` 空行, 对话框写死 480×360 —— 文字只占住上半, 下半大片空白,
行间又松。改成"三段式"(标题 / 正文 / 致谢)各是一个 QLabel, 段间距 5px、
行高 1.45, 高度按 sizeHint 实测后固定宽度即可。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from .. import version

# 内容宽度(不含两侧留白): 630 窄窗下也不挤
_WIDTH = 456


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于")
        self.setFixedWidth(_WIDTH)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 10, 18, 8)
        lay.setSpacing(4)

        title = QLabel(version.APP_NAME)
        title.setObjectName("titleLabel")
        title.setAlignment(Qt.AlignCenter)
        self.lbl_title = title
        lay.addWidget(title)

        # 正文: 版本 / 作者 / 版权 / 许可 —— 段间只留一个 <br>, 靠行高 1.45 撑开
        info = QLabel(
            "<div style='line-height:1.3; text-align:center'>"
            f"版本: <b>{version.VERSION}</b> (Build {version.BUILD})<br>"
            f"作者: {version.AUTHOR}<br>"
            f"<b>{version.COPYRIGHT}</b><br>{version.LICENSE_NOTE}"
            "</div>"
        )
        info.setAlignment(Qt.AlignCenter)
        info.setTextFormat(Qt.RichText)
        info.setWordWrap(True)
        lay.addWidget(info)

        thanks = QLabel(
            "<div style='line-height:1.3; text-align:center'>"
            "<span style='color:#8b909a'>"
            "基于 REA 逆向分析思路, 原生复刻以下开源项目核心能力:<br>"
            "· 猫抓 cat-catch —— 网页视频嗅探<br>"
            "· N_m3u8DL-CLI —— m3u8 下载引擎<br>"
            "· FFmpeg —— 实时预览解码与分片合并"
            "</span></div>"
        )
        thanks.setAlignment(Qt.AlignCenter)
        thanks.setTextFormat(Qt.RichText)
        thanks.setWordWrap(True)
        lay.addWidget(thanks)

        btn = QPushButton("确定")
        btn.setFixedWidth(100)
        btn.clicked.connect(self.accept)
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.addStretch(1)
        h.addWidget(btn)
        h.addStretch(1)
        lay.addLayout(h)

        # 宽度写死(内容固定几行, 不需要拉伸); **高度不在这里定** —— 此刻
        # QSS 还没 polish, 量到的是默认字号下的 hint, 会比实际小一圈。
        self.setFixedWidth(_WIDTH)

    def showEvent(self, event):
        """显示时再定高度: 收掉标题行多余的行盒, 再按实测 sizeHint 固定。

        标题是 17px 粗体, QLabel 的 sizeHint 却给出 45px 高(字高才 22px)——
        整个对话框里最大的一块空白就在标题下面。QSS 字体要等 polish 之后
        才生效, 所以只能放在这里量; 顺带把高度也按真实字号重新算一次。
        """
        super().showEvent(event)
        want = self.lbl_title.fontMetrics().height() + 4
        if self.lbl_title.height() != want:
            self.lbl_title.setFixedHeight(want)
        self.setFixedHeight(self.sizeHint().height())
