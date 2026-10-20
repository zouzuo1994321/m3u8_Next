"""验证页面注入脚本(w3u8dl/webinject.py)的两项能力。

1. 编解码器伪装: QtWebEngine 原生 isTypeSupported('avc1') 为 false(实测),
   注入后页面应看到 true —— 这是站点播放器愿意发起 m3u8 请求的前提。
2. URL 捕获: 页面内的 fetch/XHR 地址应被记录到 window.__M3U8DL_URLS。

用法: QT_QPA_PLATFORM=offscreen python tests/test_webinject.py
"""
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication

from m3u8dl.ui.main_window import MainWindow
from m3u8dl.webinject import READ_JS, install

PAGE = """<!DOCTYPE html><html><body>
<script>
  // 页面主动请求一个清单地址(模拟播放器行为)
  try { fetch('/stream/room.m3u8').catch(function(){}); } catch (e) {}
  try { var x = new XMLHttpRequest(); x.open('GET', '/api/playlist/123'); } catch (e) {}
</script>
</body></html>"""


class H(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/page"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n" \
               b"#EXTINF:2.0,\nseg1.ts\n#EXT-X-ENDLIST\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


PROBE = r"""JSON.stringify({
  h264: MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E"'),
  aac: MediaSource.isTypeSupported('audio/mp4; codecs="mp4a.40.2"'),
  vp9: MediaSource.isTypeSupported('video/mp4; codecs="vp09.00.10.08"'),
  injected: !!window.__m3u8dl_installed,
  urls: window.__M3U8DL_URLS || []
})"""


def main():
    import json

    src = Path(__file__).resolve().parent.parent / "tools"
    src.mkdir(exist_ok=True)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    app = QApplication(sys.argv)
    win = MainWindow()
    win._pump_queue = lambda: None          # 避免真的启动下载

    state = {"done": False}

    def check():
        def on_js(res):
            if state["done"]:
                return
            state["done"] = True
            d = json.loads(res) if res else {}
            print("=== 注入效果 ===")
            print("注入已生效   :", d.get("injected"))
            print("H.264 伪装修复:", d.get("h264"))
            print("AAC 伪装修复  :", d.get("aac"))
            print("VP9 原生支持  :", d.get("vp9"))
            urls = d.get("urls") or []
            print("捕获地址数    :", len(urls))
            for u in urls[:6]:
                print("   -", u)

            ok = True
            if not d.get("injected"):
                print("[FAIL] 注入脚本未生效"); ok = False
            else:
                print("[PASS] 注入脚本已生效(DocumentCreation/MainWorld)")
            if d.get("h264") and d.get("aac"):
                print("[PASS] H.264/AAC 已伪装为支持")
            else:
                print("[FAIL] 编解码器伪装未生效"); ok = False
            hit = [u for u in urls if "room.m3u8" in u or "playlist" in u]
            if hit:
                print("[PASS] URL 钩子捕获到页面请求")
            else:
                print("[FAIL] 未捕获页面请求"); ok = False
            print("\nWEBINJECT TEST", "OK" if ok else "FAILED")
            app.exit(0 if ok else 1)

        def fallback():
            if not state["done"]:
                state["done"] = True
                print("[FAIL] 页面探测超时")
                app.exit(1)

        win.web.page().runJavaScript(PROBE, 0, on_js)
        QTimer.singleShot(4000, fallback)

    def on_load(ok):
        # 给页面内的 fetch/XHR 一点时间落库
        QTimer.singleShot(800, check)

    win.web.loadFinished.connect(on_load)
    win.web.setUrl(QUrl(f"http://127.0.0.1:{port}/page"))
    QTimer.singleShot(20000, lambda: app.exit(1))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
