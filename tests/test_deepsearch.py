"""深度搜索测试: 验证内存级钩子能从"不存在的网络请求"里把地址/文本/密钥捞出来。

用法: python tests/test_deepsearch.py

模拟站点刻意**不发任何 m3u8 请求**，只把数据在 JS 内存里流转：
- ``String.fromCharCode`` 逐字拼出 m3u8 文本
- ``JSON.parse`` 结果里藏着视频地址
- 16 字节数组 / 32 位 hex 字符串承载 KEY
- ``<script>`` 源码里写死一个地址
- ``atob`` 解出 base64 形式的 m3u8

普通网络嗅探在这种页面上抓不到任何东西（对照组会验证这一点）。
"""
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

from m3u8dl import deepsearch as ds
from m3u8dl.websniff import VideoUrlInterceptor

# 页面里"算"出来的 m3u8（拼装后应被 fromCharCode 钩子捕获）
M3U8_LINES = [
    "#EXTM3U",
    "#EXT-X-VERSION:3",
    "#EXT-X-TARGETDURATION:10",
    "#EXT-X-MEDIA-SEQUENCE:0",
    "#EXTINF:10.0,",
    "https://cdn.example.com/live/seg1.ts",
    "#EXTINF:10.0,",
    "https://cdn.example.com/live/seg2.ts",
    "#EXT-X-ENDLIST",
]
M3U8_TEXT = "\n".join(M3U8_LINES)

# 16 字节 KEY（页面里以 number 数组形式处理）
KEY_BYTES = [0x01, 0x23, 0x45, 0x67, 0x89, 0xAB, 0xCD, 0xEF,
             0xFE, 0xDC, 0xBA, 0x98, 0x76, 0x54, 0x32, 0x10]
HEX_KEY = "0123456789abcdeffedcba9876543210"

# base64 形式的 m3u8（atob 钩子应解出来）—— 内容刻意与上面不同，
# 以便验证两条通路都能独立捕获（相同内容会被去重）
M3U8_TEXT2 = "\n".join([
    "#EXTM3U",
    "#EXT-X-VERSION:3",
    "#EXTINF:6.0,",
    "https://cdn.example.com/vod/b64seg1.ts",
    "#EXTINF:6.0,",
    "https://cdn.example.com/vod/b64seg2.ts",
    "#EXT-X-ENDLIST",
])
import base64 as _b64

B64_M3U8 = _b64.b64encode(M3U8_TEXT2.encode()).decode()

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>deep-test</title>
<script>
// 源码里写死的地址（静态扫描应命中）
var CONFIG = { hls: "https://static.example.com/hardcoded/index.m3u8" };
</script>
</head>
<body>
<script>
window.__DONE = false;

// 1) JSON.parse 里藏地址与 key
var payload = '{"data":{"playback":{"url":"https://api.example.com/a/b/play.m3u8",' +
              '"key":"0123456789abcdeffedcba9876543210"}}}';
JSON.parse(payload);

// 2) fromCharCode 拼出 m3u8（真实播放器是"整段解码再拼接"，故按行整段解码）
var lines = __LINES__;
var acc = "";
for (var i = 0; i < lines.length; i++) {
  var s = lines[i] + (i === lines.length - 1 ? "" : "\\n");
  var codes = [];
  for (var j = 0; j < s.length; j++) { codes.push(s.charCodeAt(j)); }
  acc += String.fromCharCode.apply(null, codes);   // 一次解一整行
}
window.__ACC_LEN = acc.length;

// 3) 16 字节数组当 KEY（走 TypedArray 钩子）
var k = new Uint8Array(__KEY__);

// 4) atob 解出 base64 的 m3u8
var decoded = atob(__B64__);
window.__DECODED_LEN = decoded.length;

// 5) 断言页面本身没有发出任何 m3u8 网络请求
setTimeout(function () { window.__DONE = true; }, 300);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (PAGE
                .replace("__LINES__",
                         "[" + ",".join('"%s"' % ln for ln in M3U8_LINES) + "]")
                .replace("__KEY__", "[" + ",".join(str(b) for b in KEY_BYTES) + "]")
                .replace("__B64__", '"%s"' % B64_M3U8)
                ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    app = QApplication(sys.argv)
    profile = QWebEngineProfile.defaultProfile()

    # 对照组: 只装网络拦截器, 不装深度搜索
    interceptor = VideoUrlInterceptor()
    profile.setUrlRequestInterceptor(interceptor)

    ok = ds.install(profile)
    assert ok, "深度搜索脚本注入失败"
    print("[PASS] 深度搜索脚本已注入(DocumentCreation/MainWorld/子框架)")

    view = QWebEngineView()

    class _Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, lvl, msg, line, src):
            print(f"    [页面控制台] {msg}", flush=True)

    view.setPage(_Page(profile, view))
    result = {}

    def grab():
        view.page().runJavaScript('(window.__DONE ? "1" : "0")', _on_done)

    def _on_done(v):
        if str(v) == "1":
            view.page().runJavaScript(ds.READ_JS, _on_read)
        else:
            QTimer.singleShot(120, grab)

    def _on_read(raw):
        result["raw"] = raw
        app.quit()

    def _dump_state():
        """超时兜底: 把页面内部状态打出来，便于定位注入是否生效。"""
        view.page().runJavaScript(
            "JSON.stringify({ready:!!window.__M3U8DL_DEEP_READY,"
            "done:!!window.__DONE,"
            "urls:(window.__M3U8DL_DEEP?window.__M3U8DL_DEEP.urls.length:-1),"
            "keys:(window.__M3U8DL_DEEP?window.__M3U8DL_DEEP.keys.length:-1),"
            "texts:(window.__M3U8DL_DEEP?window.__M3U8DL_DEEP.texts.length:-1),"
            "acc:window.__ACC_LEN||0,dec:window.__DECODED_LEN||0})",
            lambda r: (print(f"    [超时快照] {r}", flush=True), app.quit()))

    view.loadFinished.connect(
        lambda ok: QTimer.singleShot(200, grab))
    view.setUrl(QUrl(f"http://127.0.0.1:{port}/"))
    view.resize(320, 240)
    view.show()
    QTimer.singleShot(12000, _dump_state)
    QTimer.singleShot(20000, app.quit)
    app.exec()

    data = ds.parse_result(result.get("raw", ""))
    urls, keys, texts = data["urls"], data["keys"], data["texts"]
    print(f"    捕获: {len(urls)} 个地址, {len(keys)} 个疑似密钥, "
          f"{len(texts)} 段 m3u8 文本")
    for _t in texts:
        print(f"      text len={len(_t)} head={_t[:50]!r}", flush=True)

    # ---- 对照组: 网络层应该抓不到东西 ----
    net = interceptor.take_items()
    m3u8_net = [x for x in net if "m3u8" in x.get("url", "")]
    assert not m3u8_net, f"对照组不应有 m3u8 网络请求, 实际 {m3u8_net}"
    print("[PASS] 对照组: 该页面确实没有任何 m3u8 网络请求(普通嗅探必然为空)")

    # ---- 深度搜索: JSON.parse 里的地址 ----
    assert any("api.example.com" in u for u in urls), \
        f"应捕获 JSON.parse 里的地址, 实际 {urls}"
    print("[PASS] JSON.parse 结果中的视频地址已捕获")

    # ---- 静态 script 源码地址 ----
    assert any("static.example.com" in u for u in urls), \
        f"应捕获 script 源码里的地址, 实际 {urls}"
    print("[PASS] <script> 源码中写死的地址已捕获")

    # ---- fromCharCode 拼出的 m3u8 文本 ----
    assert any(M3U8_TEXT[:60] in t for t in texts), \
        f"应捕获 fromCharCode 拼出的 m3u8, 实际片段: {[t[:40] for t in texts]}"
    print("[PASS] String.fromCharCode 逐字拼出的 m3u8 文本已捕获")

    # ---- atob 解出的 m3u8 ----
    joined = "\n".join(texts)
    assert joined.count("#EXTM3U") >= 2, \
        f"应同时捕获 fromCharCode 与 atob 两路 m3u8, 实际 #EXTM3U 出现 {joined.count('#EXTM3U')} 次"
    print("[PASS] atob 解出的 base64 m3u8 也已捕获(共 "
          f"{joined.count('#EXTM3U')} 段)")

    # ---- 密钥规范化 ----
    cands = ds.key_candidates(keys)
    assert cands, f"应能从疑似密钥中还原出 16 字节 KEY, 原始: {keys}"
    expect = bytes(KEY_BYTES)
    expect2 = bytes.fromhex(HEX_KEY)
    assert expect in cands or expect2 in cands, \
        f"应包含页面里的 16 字节 KEY, 实际 {[c.hex() for c in cands]}"
    print(f"[PASS] 疑似密钥规范化为 16 字节: {len(keys)} 条 -> "
          f"{len(cands)} 个候选 KEY")

    # ---- 解析器容错 ----
    assert ds.parse_result("") == {"urls": [], "keys": [], "texts": []}
    assert ds.parse_result("not json")["urls"] == []
    print("[PASS] 结果解析器对空值/脏数据容错")

    srv.shutdown()
    print("\nDEEP SEARCH TEST OK: 内存级地址/文本/密钥捕获全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
