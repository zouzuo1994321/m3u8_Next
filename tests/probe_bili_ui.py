"""真实站点验证: 主窗口「解析网页」走 B站 分支 → 列表出现 DASH 清晰度。

用法: python tests/probe_bili_ui.py [视频页地址]
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--autoplay-policy=no-user-gesture-required")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from m3u8dl.ui.main_window import MainWindow
from m3u8dl.ui.theme import DARK_QSS

URL = sys.argv[1] if len(sys.argv) > 1 else \
    "https://www.bilibili.com/video/BV1HCpS6iEXh"


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)
    win = MainWindow()
    win.show()

    win.ed_url.setText(URL)
    win._on_sniff()

    deadline = time.time() + 90
    while time.time() < deadline and not win.infos:
        app.processEvents()
        time.sleep(0.1)

    n = win.tree.topLevelItemCount()
    print(f"列表条目: {n}")
    for i in range(n):
        it = win.tree.topLevelItem(i)
        info = it.data(0, Qt.UserRole)
        print(f"  [{it.text(0)}] {it.text(1)} {it.text(2)} {it.text(3)} "
              f"{it.text(4)} audio={'yes' if getattr(info, 'audio_url', '') else 'no'}")

    assert n >= 1, "B站 解析结果为空"
    infos = [win.tree.topLevelItem(i).data(0, Qt.UserRole)
             for i in range(n)]
    assert all(i is not None for i in infos), "列表条目应携带 VideoInfo"
    assert any(getattr(i, "audio_url", "") for i in infos), \
        "应至少有一条带独立音轨(DASH)"
    win.close()
    app.processEvents()
    print("RESULT=OK")


if __name__ == "__main__":
    main()
