"""深色主题 QSS —— 墨黑底 / 深蓝主色 / 鎏金点缀。

v1.1.14: 主色由朱红(#a93226/#c0392b)整体换成**深蓝**(#1e40af/#2b57cf)。
红蓝只改这一处和几处硬编码, 全站配色都从这里的 accent 派生。
"""

DARK_QSS = """
* { font-family: "Microsoft YaHei", "Segoe UI", sans-serif; }
QWidget { background-color: #14161b; color: #e6e3dd; font-size: 13px; }
QMainWindow, QDialog { background-color: #14161b; }
QGroupBox {
    background-color: #1a1d24; border: 1px solid #2c313c; border-radius: 8px;
    margin-top: 10px; padding: 14px 10px 10px 10px; font-weight: bold;
}
QGroupBox::title {
    subcontrol-origin: margin; subcontrol-position: top left;
    left: 12px; padding: 0 6px; color: #d4af37;
}
/* v1.1.16: 「下载进度」只有两行内容, 沿用 10px 下内边距会让「就绪」下面
   空出一截(用户反馈"压缩就绪下方留白")。单独把下内边距收到 2px。 */
QGroupBox#progGroup { padding: 14px 10px 2px 10px; }
QPushButton {
    background-color: #262b36; border: 1px solid #3a4150; border-radius: 6px;
    padding: 6px 16px; color: #e6e3dd; min-height: 18px;
}
QPushButton:hover { background-color: #323949; border-color: #4a5265; }
QPushButton:pressed { background-color: #1f242e; }
QPushButton:disabled { background-color: #1c1f26; color: #5a5f6a; border-color: #262b36; }
QPushButton#primaryBtn { background-color: #1e40af; border-color: #3b6fe0; color: #ffffff; font-weight: bold; }
QPushButton#primaryBtn:hover { background-color: #2b57cf; }
QPushButton#primaryBtn:disabled { background-color: #1b2740; color: #7b879c; border-color: #1b2740; }
QPushButton#dangerBtn { background-color: #1b3355; border-color: #29507e; }
QPushButton#dangerBtn:hover { background-color: #29507e; }
QLineEdit, QSpinBox, QPlainTextEdit, QTextEdit {
    background-color: #1d2129; border: 1px solid #363d4a; border-radius: 6px;
    padding: 5px 8px; color: #e6e3dd; selection-background-color: #1e40af;
}
QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color: #3b6fe0; }
QCheckBox { spacing: 8px; background: transparent; }
QCheckBox::indicator {
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid #4a5265; background: #1d2129;
}
QCheckBox::indicator:checked { background: #1e40af; border-color: #3b6fe0; }
QProgressBar {
    background-color: #1d2129; border: 1px solid #363d4a; border-radius: 6px;
    height: 18px; text-align: center; color: #e6e3dd; font-size: 12px;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #16306b, stop:1 #2f6bd8); border-radius: 5px;
}
QTreeWidget, QTableWidget {
    background-color: #1a1d24; border: 1px solid #2c313c; border-radius: 8px;
    alternate-background-color: #1f232c;
}
QTreeWidget::item, QTableWidget::item { padding: 4px 2px; }
QTreeWidget::item:selected, QTableWidget::item:selected {
    background-color: #1c2f52; color: #dce8ff;
}
QHeaderView::section {
    background-color: #22262f; color: #d4af37; padding: 5px 6px;
    border: none; border-right: 1px solid #2c313c;
}
QLabel { background: transparent; }
QLabel#titleLabel { font-size: 17px; font-weight: bold; color: #d4af37; }
QLabel#hintLabel { color: #8b909a; }
QStatusBar { background-color: #101216; color: #8b909a; }
QStatusBar::item { border: none; }
QMenuBar { background-color: #14161b; color: #e6e3dd; }
QMenuBar::item:selected { background-color: #262b36; }
QMenu { background-color: #1a1d24; border: 1px solid #2c313c; }
QMenu::item:selected { background-color: #1c2f52; }
QScrollBar:vertical { background: #14161b; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3a4150; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #4a5265; }
QScrollBar:horizontal { background: #14161b; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #3a4150; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QSplitter::handle { background-color: #2c313c; }
QSplitter::handle:horizontal { width: 3px; }
QSplitter::handle:vertical { height: 3px; }

/* ---- 预览区控制条: 与其它按钮同色 + 无边框 ---- */
/* v1.1.14: 用户要求「颜色和其它 UI 一致」且「删掉周边 1px 外框」——
   底色对齐普通 QPushButton(#262b36), border 直接置 none。 */
QPushButton#pvBtn {
    background-color: #262b36; border: none; border-radius: 7px;
    color: #e6e3dd; padding: 6px 12px; min-height: 20px;
}
QPushButton#pvBtn:hover { background-color: #323949; }
QPushButton#pvBtn:pressed { background-color: #1f242e; }
QPushButton#pvBtn:disabled { background-color: #1c1f26; color: #5a5f6a; }
QSlider { background: transparent; }
QSlider::groove:horizontal { height: 4px; border-radius: 2px; background: #3a3f47; }
/* v1.1.15: 音量条由紫改蓝, 与主色(深蓝)统一 */
QSlider::sub-page:horizontal { background: #2b57cf; border-radius: 2px; }
QSlider::add-page:horizontal { background: #3a3f47; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 10px; height: 10px; margin: -4px 0; border-radius: 5px;
    background: #5b93f0; border: none;
}
QSlider::handle:horizontal:hover { background: #8ab4f8; }

/* 右列「进阶功能」按钮: 横向内边距与普通按钮(16px)对齐到同一档。
   原来只有 8px, 于是「深度搜索」四个字的**起始位置**比下方「开始下载」左移 8px,
   两个按钮的边框明明齐平, 看起来却像没对齐(v1.1.17 用户反馈)。 */
QPushButton#advBtn { padding: 6px 15px; }
/* 缓存捕捉的「保存」按钮只在录制中出现, 这一行会变成 4 个按钮 —— 用窄内边距 */
QPushButton#advBtnSm { padding: 6px 8px; }
"""
