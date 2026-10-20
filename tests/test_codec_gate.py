"""复现并验证 Stripchat 类站点的 Error 102630 根因与修复。

## 场景

站点播放器先做能力检测: 若 H.264 不可解码就直接显示错误、**不再请求 m3u8**
(即真实站点 Error 102630 + 嗅探捕获为空的成因)。检测通过才 fetch 清单。

## 验证

- 对照组: 干净 profile(未注入) -> 页面判定不支持 -> 服务器收不到 m3u8 请求
- 实验组: 主窗口默认 profile(已注入伪装) -> 检测通过 -> 服务器收到 m3u8 请求

用法: QT_QPA_PLATFORM=offscreen python tests/test_codec_gate.py
"""
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication

PAGE = """<!DOCTYPE html><html><body><div id="st">checking</div>
<script>
  var ok = window.MediaSource &&
           MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E"');
  if (!ok) {
    // 真实站点的行为: 直接报错, 不发起任何媒体请求
    document.getElementById('st').textContent = 'ERROR 102630';
  } else {
    document.getElementById('st').textContent = 'PLAYING';
    fetch('/live/stream.m3u8').catch(function(){});
  }
</script></body></html>"""

HITS = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS.append(self.path)
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


def main():
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    page_url = f"http://127.0.0.1:{port}/page"

    app = QApplication(sys.argv)

    result = {}

    # ---------- 对照组: 未注入的干净 profile ----------
    ctrl_profile = QWebEngineProfile()          # off-the-record, 无注入脚本
    ctrl_page = QWebEnginePage(ctrl_profile)

    def ctrl_load(ok):
        QTimer.singleShot(1500, ctrl_done)

    def ctrl_done():
        result["ctrl_hit"] = any("/live/stream.m3u8" in h for h in HITS)
        result["ctrl_text"] = "?"
        ctrl_page.runJavaScript(
            "document.getElementById('st').textContent", 0, lambda t: (
                result.__setitem__("ctrl_text", t), start_experiment()))

    ctrl_page.loadFinished.connect(ctrl_load)
    ctrl_page.setUrl(QUrl(page_url))

    # ---------- 实验组: 主窗口(默认 profile, 已注入伪装) ----------
    def start_experiment():
        HITS.clear()
        from m3u8dl.ui.main_window import MainWindow

        win = MainWindow()
        win._pump_queue = lambda: None
        win._web_sniff.SETTLE_MS = 2500      # 缩短沉降, 加速测试
        result["win"] = win

        captured = {"urls": []}

        def on_hit(u, t):
            captured["urls"].append(u)

        win._url_interceptor.hit.connect(on_hit)

        def exp_load(ok):
            QTimer.singleShot(2500, exp_done)

        def exp_done():
            win.web.page().runJavaScript(
                "document.getElementById('st').textContent", 0, finish)

        def finish(text):
            result["exp_hit"] = any("/live/stream.m3u8" in h for h in HITS)
            result["exp_text"] = text
            result["exp_urls"] = list(captured["urls"])
            report()

        win.web.loadFinished.connect(exp_load)
        win._start_web_sniff(page_url)

    def report():
        print("=== 编解码器门禁实验 ===")
        print(f"对照组(未注入) 页面状态: {result.get('ctrl_text')!r}")
        print(f"对照组 服务器收到 m3u8 请求: {result.get('ctrl_hit')}")
        print(f"实验组(已注入) 页面状态: {result.get('exp_text')!r}")
        print(f"实验组 服务器收到 m3u8 请求: {result.get('exp_hit')}")
        print(f"实验组 拦截器捕获地址: {result.get('exp_urls')}")
        print()
        ok = True
        if result.get("ctrl_hit") is False:
            print("[PASS] 对照组复现故障: 编码器检测失败 -> 不请求 m3u8")
        else:
            print("[WARN] 对照组也发出了请求, 该环境可能已内置 H.264"); ok = False
        if result.get("exp_hit") is True:
            print("[PASS] 注入后站点检测通过 -> m3u8 请求发出并可被捕获")
        else:
            print("[FAIL] 注入后仍未捕获到 m3u8"); ok = False

        win = result.get("win")
        if win is not None:
            try:
                win.preview.stop()
            except Exception:
                pass
        print("\nCODEC GATE TEST", "OK" if ok else "FAILED")
        app.exit(0 if ok else 1)

    QTimer.singleShot(30000, lambda: app.exit(1))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
