"""启动链路验证: 卡片 -> 主窗口淡入 的完整时序是否走得通。

检查项:
- 卡片在开始时可见
- 动效结束后卡片隐藏、主窗口可见且 windowOpacity 回到 1.0
- 主窗口图标不是空的(应用图标已生效)

用法: python tests/probe_splash_flow.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from m3u8dl import version  # noqa: E402
from m3u8dl.ui.main_window import MainWindow, assets_dir  # noqa: E402
from m3u8dl.ui.splash import build_splash, reveal_window  # noqa: E402
from m3u8dl.ui.theme import DARK_QSS  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "shots"
RESULT = []


def chk(cond, msg):
    RESULT.append(bool(cond))
    print(("OK   " if cond else "FAIL ") + msg)


app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(DARK_QSS)

splash = build_splash(assets_dir(), version.APP_NAME,
                      f"{version.VERSION} · Build {version.BUILD}")
if splash is None:
    print("RESULT=FAIL 无法构建启动卡片")
    sys.exit(1)

state = {}


def reveal():
    state["revealed"] = True
    reveal_window(win)


splash.play(reveal)
app.processEvents()
win = MainWindow()
chk(splash.isVisible(), "启动卡片已显示")
splash.grab().save(str(OUT / "splash_live.png"))


def step_mid():
    chk(not state.get("revealed"), "1.0s 时动效尚未结束(时序正常)")
    chk(splash.isVisible(), "1.0s 时卡片仍可见")
    chk(splash._t > 0.2, f"动画进度在推进 (t={splash._t:.2f})")
    splash.grab().save(str(OUT / "splash_mid.png"))


def step_end():
    chk(state.get("revealed"), "动效结束后触发了主窗口显示")
    chk(win.isVisible(), "主窗口已显示")
    chk(not splash.isVisible(), "卡片已隐藏")
    chk(abs(win.windowOpacity() - 1.0) < 1e-3,
        f"主窗口不透明 (opacity={win.windowOpacity():.3f})")
    chk(not win.windowIcon().isNull(), "主窗口图标不为空")
    sizes = [s.width() for s in win.windowIcon().availableSizes()]
    print(f"     icon 可用尺寸: {sorted(sizes)}")
    win.grab().save(str(OUT / "splash_after.png"))
    print(f"\n{sum(RESULT)}/{len(RESULT)} passed")
    print("RESULT=" + ("OK" if all(RESULT) else "FAIL"))
    app.exit(0)


QTimer.singleShot(1000, step_mid)
QTimer.singleShot(2600, step_end)
app.exec()
