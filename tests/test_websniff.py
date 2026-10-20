"""集成测试: 验证 WebEngine 网络请求拦截能捕获 JS 动态加载的视频地址。

模拟 Stripchat 类直播站的特征: HTML 源码中不含任何视频直链, 视频清单由
JS 在页面加载后延迟经 XHR 请求——静态正则嗅探抓不到, 必须靠请求拦截。
同时验证: 图片/脚本等噪音请求被正确过滤, 拦截结果可被 sniff_urls 解析。

用法: QT_QPA_PLATFORM=offscreen python tests/test_websniff.py
"""
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView

from m3u8dl.sniffer import Sniffer
from m3u8dl.websniff import VideoUrlInterceptor, WebSniffSession

# ---------------------------------------------------------------- 测试页面
# 关键: 源码中没有任何 m3u8/mp4 直链, 全部由 JS 动态请求
PAGE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Test Live Room 001</title></head>
<body>
<div id="player">loading...</div>
<img src="/img/logo.png" width="10">
<script>
// 模拟播放器: 页面加载 1.5s 后经 XHR 获取清单(Stripchat 类站点同款行为)
setTimeout(function() {
    fetch('/stream/room123.m3u8').then(function(r) { return r.text(); })
    .then(function(t) {
        document.getElementById('player').textContent = 'manifest ' + t.length;
    });
}, 1500);
// 再延迟 2.5s 加载一个 mp4 直媒体(验证 ResourceTypeMedia 拦截)
setTimeout(function() {
    var v = document.createElement('video');
    v.preload = 'auto';
    v.src = '/media/clip.mp4';
    document.body.appendChild(v);
}, 2500);
</script>
</body></html>
"""

M3U8_TEXT = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:4.0,
/seg/0.ts
#EXTINF:4.0,
/seg/1.ts
#EXTINF:4.0,
/seg/2.ts
#EXT-X-ENDLIST
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/page"):
            self._send(PAGE_HTML.encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/stream/"):
            self._send(M3U8_TEXT.encode(), "application/vnd.apple.mpegurl")
        elif self.path.startswith("/seg/"):
            self._send(b"\x47" * 188, "video/mp2t")
        elif self.path.startswith("/media/"):
            self._send(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 300, "video/mp4")
        elif self.path.startswith("/img/"):
            self._send(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, "image/png")
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    page_url = f"http://127.0.0.1:{port}/page"
    print(f"测试服务: {page_url}")

    app = QApplication(sys.argv)
    app.setApplicationName("websniff-test")

    view = QWebEngineView()
    interceptor = VideoUrlInterceptor()
    QWebEngineProfile.defaultProfile().setUrlRequestInterceptor(interceptor)
    session = WebSniffSession(interceptor)
    view.page().loadFinished.connect(session.notify_page_loaded)

    captured = {"items": None}
    session.finished.connect(lambda items: captured.update(items=items))

    # ---- 启动浏览器嗅探(与主窗口流程一致) ----
    session.start()
    view.setUrl(QUrl(page_url))
    print("页面加载中, 等待沉降监听结束…")

    deadline = time.time() + 30
    while time.time() < deadline and session.running:
        app.processEvents()
        time.sleep(0.05)

    items = captured["items"] or []
    urls = [i["url"] if isinstance(i, dict) else i for i in items]
    print(f"\n拦截到的地址 {len(urls)} 个:")
    for u in urls:
        print(f"  - {u}")

    # ---- 断言 0: 捕获项应带有浏览器真实请求头(供 403 防盗链场景回放) ----
    with_hdrs = [i for i in items
                 if isinstance(i, dict) and (i.get("headers") or {}).get("User-Agent")]
    assert with_hdrs, "失败: 拦截项未携带真实请求头"
    hd = with_hdrs[0]["headers"]
    print(f"[PASS] 请求头捕获: {len(hd)} 项, Referer="
          f"{hd.get('Referer') or hd.get('referer') or '(无)'}")

    # ---- 断言 1: 动态加载的 m3u8 必须被捕获 ----
    m3u8_hits = [u for u in urls if ".m3u8" in u]
    assert m3u8_hits, "失败: 未拦截到 JS 动态加载的 m3u8 清单"
    print(f"\n[PASS] 动态 m3u8 拦截: {m3u8_hits[0]}")

    # ---- 断言 2: 噪音请求(图片)不应被收集 ----
    noise = [u for u in urls if "/img/" in u or u.endswith(".png")]
    assert not noise, f"失败: 噪音请求未被过滤: {noise}"
    print("[PASS] 图片等噪音请求已过滤")

    # ---- 断言 3: 拦截结果可被 sniff_urls 解析为 VideoInfo ----
    sniffer = Sniffer(use_proxy=False, log=lambda m: print(f"  [sniff] {m}"))
    infos = sniffer.sniff_urls(urls, page_url=page_url, page_title="Test Live Room 001")
    assert infos, "失败: sniff_urls 未解析出任何视频信息"
    info = infos[0]
    assert ".m3u8" in info.url, f"失败: 解析结果不是 m3u8: {info.url}"
    assert info.title == "Test Live Room 001", f"标题错误: {info.title}"
    assert info.duration_text and info.duration_text != "未知", \
        f"时长解析失败: {info.duration_text}"
    print(f"[PASS] sniff_urls 解析: 标题={info.title!r} 时长={info.duration_text} "
          f"大小={info.size_text}")

    # ---- 断言 4(附加): mp4 媒体类型拦截 ----
    mp4_hits = [u for u in urls if ".mp4" in u]
    if mp4_hits:
        print(f"[PASS] ResourceTypeMedia 拦截: {mp4_hits[0]}")
    else:
        print("[INFO] mp4 媒体请求未被捕获(浏览器可能未发起, 不影响主链路)")

    server.shutdown()
    print("\nWEBSNIFF TEST OK: 动态加载视频的拦截 + 解析链路全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
