"""端到端验证: 主窗口「系统浏览器」按钮 -> 捕获 -> 自动解析 -> 表格出条目。

会启动本机 Edge(独立 profile) 并打开页面, 约 30 秒后自动结束。
用法:
    python tests/probe_cdp_ui.py [页面地址]
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--autoplay-policy=no-user-gesture-required")
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from m3u8dl.ui.main_window import MainWindow  # noqa: E402
from m3u8dl.ui.theme import DARK_QSS  # noqa: E402

PAGE = (sys.argv[1] if len(sys.argv) > 1
        else "https://livecamrips.to/video/16401088")
WATCH_SECONDS = 26

app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(DARK_QSS)
try:
    from PySide6.QtWebEngineCore import QWebEngineProfile
    _p = QWebEngineProfile.defaultProfile()
    _p.setHttpUserAgent(re.sub(r"QtWebEngine/[\d.]+\s*", "", _p.httpUserAgent()))
except Exception:
    pass

win = MainWindow()
win.show()

logs = []
win._log_ui_orig = win._log_ui


def capture_log(msg):
    logs.append(msg)
    win._log_ui_orig(msg)


win._log_ui = capture_log

print(f"[btn] 系统浏览器 可用={win.btn_sys_browser.isEnabled()}")
win.ed_url.setText(PAGE)
QTimer.singleShot(600, lambda: win.btn_sys_browser.setChecked(True))


def report():
    rows = []
    for i in range(win.tree.topLevelItemCount()):
        it = win.tree.topLevelItem(i)
        info = it.data(0, 0x0100)   # Qt.UserRole
        rows.append((
            it.text(0)[:40], it.text(1), it.text(2), it.text(3), it.text(4),
            getattr(info, "url", "")[:90],
            bool(getattr(info, "headers", {}).get("Referer")),
        ))
    print(f"\n=== 捕获候选 {len(win._cdp_items)} 条, 表格 {len(rows)} 行 ===")
    for r in rows:
        print(f"  {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]}")
        print(f"     url={r[5]}")
        print(f"     有Referer={r[6]}")
    ok = any(r[1] not in ("—", "") for r in rows)
    print(f"\nRESULT={'OK' if rows and ok else 'EMPTY'}")
    win._closing = True
    win.close()
    app.quit()


def end_listen():
    print("[step] 结束监听, 等待解析…")
    win.btn_sys_browser.setChecked(False)
    QTimer.singleShot(9000, report)


QTimer.singleShot(WATCH_SECONDS * 1000, end_listen)
QTimer.singleShot((WATCH_SECONDS + 40) * 1000, report)
app.exec()
