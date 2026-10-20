"""临时脚本: 截图「更多功能」双页签面板, 用于 UI 核对。

不依赖浏览器引擎: 用假 hooks 灌入编码/画面源, 只看版式。
用法: python tests/shot_tools.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from m3u8dl.ui.theme import DARK_QSS
from m3u8dl.ui.tools_dialog import ToolsDialog

OUT = Path(__file__).resolve().parent.parent / "docs" / "shots"


class _Fake:
    """假 hooks: 返回固定数据, 不碰网络与页面。"""

    def record(self, kind, index, mime):
        return True, "已开始录制"

    def stop(self):
        return True, "已保存 录制_1130.webm (2.41 MB)"

    def mimes(self):
        return True, ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus",
                      "video/webm;codecs=vp9", "video/webm;codecs=vp8",
                      "video/webm", "audio/webm;codecs=opus", "audio/webm"]

    def sources(self):
        return True, [
            {"i": 0, "kind": "video", "label": "blob:https://example.com/8f2a…",
             "playing": True, "w": 1280, "h": 720},
            {"i": 1, "kind": "audio", "label": "(内存流)", "playing": True,
             "w": 0, "h": 0},
        ]

    def mobile(self, on):
        return True, ("已切换为 iPhone UA 并重载页面" if on
                      else "已还原桌面 UA 并重载页面")

    def auto(self, on):
        return True, ("已开启: 解析结束后自动下载最佳一条" if on
                      else "已关闭自动下载")

    def send(self, url):
        return True, "已发送 3 条 (HTTP 200)"

    def adblock(self, on):
        return True, ("已开启广告拦截" if on else "已关闭广告拦截")

    def stats(self):
        return True, {}

    def hooks(self):
        return {k: getattr(self, k) for k in
                ("record", "stop", "mimes", "sources", "mobile", "auto",
                 "send", "adblock", "stats")}


FRAGS = [f"https://cdn.example.com/hls/seg_{i:04d}.ts?token=abc" for i in range(1, 61)]

STATE = {
    "strip_image": True, "skip_decrypt": False, "audio_only": False,
    "extra_query": "token=abc&exp=1799999999", "selected": list(range(0, 60, 2)),
    "regex": r"seg_\d+\.ts$",
    "rec_mime": "video/webm;codecs=vp9,opus", "rec_index": 0,
    "simulate_mobile": False, "auto_download": True,
    "send_url": "http://127.0.0.1:8080/hook", "adblock": True,
}

KEYS = [b"\x00" * 16, b"0123456789abcdef", b"a1b2c3d4e5f60718"]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)

    f = _Fake()
    dlg = ToolsDialog(state=STATE, on_log=lambda m: None,
                      fetch_fragments=lambda: list(FRAGS),
                      key_candidates=KEYS, hooks=f.hooks())
    dlg.resize(820, 620)
    dlg.show()

    def shot1():
        dlg._load_fragments()
        dlg.tabs.setCurrentIndex(0)
        app.processEvents()
        dlg.grab().save(str(OUT / "tools_tab1.png"))
        QTimer.singleShot(300, shot2)

    def shot2():
        dlg.tabs.setCurrentIndex(1)
        dlg.btn_mobile.setChecked(True)
        app.processEvents()
        dlg.grab().save(str(OUT / "tools_tab2.png"))
        QTimer.singleShot(200, done)

    def done():
        print(f"saved: {OUT / 'tools_tab1.png'}")
        print(f"saved: {OUT / 'tools_tab2.png'}")
        app.quit()

    QTimer.singleShot(600, shot1)
    app.exec()


if __name__ == "__main__":
    main()
