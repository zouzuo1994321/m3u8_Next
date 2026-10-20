"""验证「请求头回放」修复 CDN 403(防盗链)。

模拟线上真实情况: 清单地址要求 Referer 来自页面 + Cookie 有效, 否则 403。
真实案例: happyams1.streambucket.xyz 只用通用 UA + Referer 请求 -> 403。

验证三点:
1. 反例: 不带浏览器请求头 -> 403, 解析不出视频
2. 正例: 回放浏览器请求头(Referer/UA/Cookie) -> 200, 解析成功
3. 集成: 主窗口嗅探后自动组装的请求头里确实带上了 Cookie

用法: QT_QPA_PLATFORM=offscreen python tests/test_headers_replay.py
"""
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication

PAGE = """<!DOCTYPE html><html><body><div id="st">ready</div>
<script>
  fetch('/stream/x.m3u8').catch(function(){});
</script></body></html>"""

M3U8 = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        "#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:4.0,\nseg1.ts\n#EXTINF:4.0,\n"
        "seg2.ts\n#EXT-X-ENDLIST\n")

STATS = {"403": 0, "200": 0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/page"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", "token=abc123; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/stream/"):
            ref = self.headers.get("Referer", "")
            cookie = self.headers.get("Cookie", "")
            ua = self.headers.get("User-Agent", "")
            ok = ("/page" in ref and "token=abc123" in cookie and bool(ua))
            if not ok:
                STATS["403"] += 1
                self.send_error(403, "Forbidden")
                return
            STATS["200"] += 1
            body = M3U8.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


def main():
    from m3u8dl.sniffer import Sniffer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    page_url = f"http://127.0.0.1:{port}/page"
    stream_url = f"http://127.0.0.1:{port}/stream/x.m3u8"

    app = QApplication(sys.argv)
    ok_all = {"v": True}

    # ---------- 1. 反例: 通用请求头 -> 403 ----------
    print("== 1. 反例: 不带浏览器请求头 ==")
    sn = Sniffer(use_proxy=False, log=lambda m: None)
    bad = sn.sniff_urls([stream_url], page_url=page_url, page_title="t")
    print(f"   解析结果: {len(bad)} 个   服务器 403 次数: {STATS['403']}")
    if not bad and STATS["403"] > 0:
        print("[PASS] 复现 403: 缺 Cookie/真实 Referer 时解析失败")
    else:
        print("[WARN] 反例未复现 403, 该环境可能不校验")
        ok_all["v"] = False

    # ---------- 2. 正例: 回放浏览器请求头 ----------
    print("\n== 2. 正例: 回放浏览器请求头 ==")
    replay = {
        stream_url: {
            "Referer": page_url,
            "Origin": f"http://127.0.0.1:{port}",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/140.0.0.0 Safari/537.36"),
            "Cookie": "token=abc123",
        }
    }
    sn2 = Sniffer(use_proxy=False, log=lambda m: None)
    good = sn2.sniff_urls([stream_url], page_url=page_url,
                          page_title="防盗链测试", headers_map=replay)
    print(f"   解析结果: {len(good)} 个   服务器 200 次数: {STATS['200']}")
    if good:
        print(f"[PASS] 回放请求头后解析成功: {good[0].duration_text}")
        if good[0].headers.get("Cookie"):
            print("[PASS] VideoInfo 携带 Cookie(下载/预览可复用)")
        else:
            print("[FAIL] VideoInfo 未携带 Cookie")
            ok_all["v"] = False
    else:
        print("[FAIL] 回放请求头后仍解析失败")
        ok_all["v"] = False

    # ---------- 3. 集成: 主窗口嗅探自动组装请求头(含 Cookie) ----------
    print("\n== 3. 集成: 主窗口嗅探 -> 自动组装请求头 ==")
    from m3u8dl.ui.main_window import MainWindow

    win = MainWindow()
    win._pump_queue = lambda: None
    win._web_sniff.SETTLE_MS = 2500

    captured = {"items": []}
    win._web_sniff.finished.connect(
        lambda items: captured.update(items=items))

    def on_load(ok):
        QTimer.singleShot(3000, probe)

    def probe():
        items = captured["items"] or []
        urls = [i["url"] if isinstance(i, dict) else i for i in items]
        hits = [u for u in urls if "/stream/x.m3u8" in u]
        if not hits:
            print(f"[FAIL] 主窗口未捕获到 m3u8, 已捕获: {urls}")
            ok_all["v"] = False
            return finish()
        # 找到对应项并组装请求头
        item = next(i for i in items
                    if isinstance(i, dict) and "/stream/x.m3u8" in i["url"])
        hdrs = win._build_headers(item, win._page_headers(page_url))
        print(f"   组装请求头: {sorted(hdrs.keys())}")
        if hdrs.get("Cookie"):
            print("[PASS] 自动附带 Cookie(来自内置浏览器)")
        else:
            print("[FAIL] 未附带 Cookie")
            ok_all["v"] = False
        if hdrs.get("Referer") and "/page" in hdrs["Referer"]:
            print(f"[PASS] Referer 正确: {hdrs['Referer']}")
        else:
            print(f"[FAIL] Referer 异常: {hdrs.get('Referer')}")
            ok_all["v"] = False
        # 用组装出的头真打一次, 必须 200
        sn3 = Sniffer(use_proxy=False, log=lambda m: None)
        before = STATS["200"]
        infos = sn3.sniff_urls([hits[0]], page_url=page_url,
                               page_title="e2e", headers_map={hits[0]: hdrs})
        if infos and STATS["200"] > before:
            print(f"[PASS] 端到端解析成功: {infos[0].duration_text}")
        else:
            print("[FAIL] 端到端解析失败")
            ok_all["v"] = False
        finish()

    def finish():
        try:
            win.preview.stop()
        except Exception:
            pass
        print("\nHEADERS REPLAY TEST", "OK" if ok_all["v"] else "FAILED")
        app.exit(0 if ok_all["v"] else 1)

    win.web.loadFinished.connect(on_load)
    win._start_web_sniff(page_url)

    QTimer.singleShot(40000, lambda: app.exit(1))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
