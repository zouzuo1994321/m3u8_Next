"""临时脚本: 抓取当前主窗口布局截图, 用于布局调整参考。

用法:
    python tests/shot_layout.py            # 用窗口自己的默认尺寸
    python tests/shot_layout.py 1200 900   # 指定宽高
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from m3u8dl.ui.main_window import MainWindow  # noqa: E402
from m3u8dl.ui.theme import DARK_QSS  # noqa: E402

W = int(sys.argv[1]) if len(sys.argv) > 2 else 0
H = int(sys.argv[2]) if len(sys.argv) > 2 else 0

app = QApplication(sys.argv)
# 与 main.py 一致: 不设 Fusion/QSS 的话截到的是系统主题, 与真实效果不符
app.setStyle("Fusion")
app.setStyleSheet(DARK_QSS)
win = MainWindow()
if W and H:
    win.resize(W, H)
win.show()


def shot():
    print(f"window={win.width()}x{win.height()}")
    print(f"minimumSizeHint={win.minimumSizeHint().width()}x"
          f"{win.minimumSizeHint().height()}")
    print(f"split sizes={win.main_split.sizes()}")
    print(f"left minw={win.main_split.widget(0).minimumSizeHint().width()} "
          f"right minw={win.main_split.widget(1).minimumSizeHint().width()}")
    out = Path(__file__).resolve().parent.parent / "docs" / "shots" / "layout_check.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    win.grab().save(str(out))
    print(f"saved: {out}")
    app.quit()


QTimer.singleShot(2500, shot)
app.exec()
