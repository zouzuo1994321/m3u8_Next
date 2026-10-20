"""录制与自动化（猫抓 6 按钮）的验证。

覆盖:
1. **编码协商**: QtWebEngine 开源版能不能录 webm(VP8/VP9/Opus)。这决定
   「视频录制 / 录制webRTC / 屏幕捕捉」是否成立。
2. **视频录制**: canvas.captureStream() -> MediaRecorder -> 分片经 QWebChannel
   回传 -> 落盘, 校验得到合法 WebM(EBML 头 ``1A 45 DF A3``)。
3. **录制webRTC**: RTCPeerConnection 被代理后仍能正常构造(不能把页面搞坏),
   且 track 钩子已挂上。
4. **RecordStore**: 连续 append + 落盘 + 空录制不产出文件 + 重名自动加序号。
5. **自动下载**: 打开开关后, 解析结果自动入队且只入一次。
6. **数据发送**: POST 到本地服务, 校验载荷字段。
7. **模拟手机**: UA 切到 iPhone 再还原。

用法: QT_QPA_PLATFORM=offscreen python tests/test_recorder.py
"""
import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--autoplay-policy=no-user-gesture-required")

from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtWidgets import QApplication

from m3u8dl import recorder
from m3u8dl.recorder import RecordStore
from m3u8dl.sniffer import VideoInfo
from m3u8dl.ui.main_window import MainWindow

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".test_work" / "recorder"

PASS, FAIL = [], []


def check(ok, msg):
    (PASS if ok else FAIL).append(msg)
    print(f"[{'PASS' if ok else 'FAIL'}] {msg}")


#: 用 canvas 造一路 25fps 的假视频流（不依赖 H.264 解码器）
PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>recorder test</title></head><body>
<canvas id="c" width="320" height="180"></canvas>
<script>
var cv = document.getElementById('c'), ctx = cv.getContext('2d'), n = 0;
setInterval(function () {
  n++;
  ctx.fillStyle = 'hsl(' + (n * 7 % 360) + ',70%,50%)';
  ctx.fillRect(0, 0, 320, 180);
  ctx.fillStyle = '#fff';
  ctx.font = '28px sans-serif';
  ctx.fillText('frame ' + n, 20, 100);
}, 40);
window.__canvasStream = null;
window.__startCanvasStream = function () {
  if (!window.__canvasStream) { window.__canvasStream = cv.captureStream(25); }
  return !!window.__canvasStream;
};
</script></body></html>
"""


class _Page(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Hook(http.server.BaseHTTPRequestHandler):
    """接收「数据发送」POST 的服务。"""
    protocol_version = "HTTP/1.1"
    received = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n)
        try:
            type(self).received.append(json.loads(raw.decode("utf-8")))
        except Exception:
            type(self).received.append({"_raw": raw[:200].decode("utf-8", "ignore")})
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler):
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _wait(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def run_js(win, js, timeout=3000):
    """执行页面 JS 并等回调返回。"""
    st = {"done": False, "val": None}

    def cb(v):
        st["done"] = True
        st["val"] = v

    win.web.page().runJavaScript(js, 0, cb)
    deadline = time.monotonic() + timeout / 1000
    while not st["done"] and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.02)
    return st["val"]


# ---------------------------------------------------------------- 用例
def test_mime_support(win):
    raw = run_js(win, recorder.MIMES_JS)
    mimes = recorder.parse_mimes(raw)
    check(bool(mimes), f"页面报告可用录制编码 {len(mimes)} 种")
    check(any("vp8" in m or "vp9" in m for m in mimes),
          "存在 VP8/VP9 webm 编码(开源版 QtWebEngine 不含 H.264)")


def test_record_canvas_stream(win):
    """视频录制链路: captureStream -> MediaRecorder -> 桥 -> 落盘 -> 合法 WebM。"""
    ok = run_js(win, "window.__startCanvasStream()")
    check(bool(ok), "canvas.captureStream(25) 已就绪")

    win._rec.abort()
    win._rec.begin("element")
    win._rec_kind = ""            # 绕过 UI 状态, 直接驱动录制脚本

    # 直接对 canvas 流起录（不依赖页面里的 <video> 元素）。
    # 先清空轨道池: 上一个用例塞进来的是一条**从未绘制**的空白 canvas 轨道,
    # canvas 不重绘就不产帧, MediaRecorder 会一片都录不到。
    js = ("(function(){"
          "var R=window.__M3U8DL_REC;"
          "R.tracks.video.length=0; R.tracks.audio.length=0;"
          "var s=window.__canvasStream;"
          "R.tracks.video.push(s.getVideoTracks()[0]);"
          "return window.__M3U8DL_REC_START({kind:'webrtc',videoBits:800000});"
          "})()")
    res = run_js(win, js)
    check(res == "started", f"MediaRecorder 已启动(返回 {res!r})")

    _wait(3000)                   # 录 3 秒, 每秒一片
    run_js(win, recorder.STOP_JS)
    _wait(1200)                   # 等最后一片回传落盘

    size, chunks = win._rec.bytes, win._rec.chunks
    check(chunks >= 1 and size > 1000,
          f"录制分片经 QWebChannel 回传({chunks} 片 / {size} 字节)")

    out = WORK / "out"
    path = win._rec.finish(out, "rec_canvas", "video/webm",
                           on_log=lambda m: None)
    check(path is not None and path.exists(), "录制文件已落盘")
    if path:
        head = path.read_bytes()[:4]
        check(head == b"\x1a\x45\xdf\xa3",
              f"输出是合法 WebM(EBML 头 {head.hex()})")
        check(path.suffix == ".webm", f"扩展名跟随编码: {path.name}")
    win._rec.abort()


def test_rtc_proxy_alive(win):
    """代理 RTCPeerConnection 后不能把页面搞坏。

    注意: ``runJavaScript`` 无法把 JS 对象转回 Python(会得到空串), 所以一律
    用 ``JSON.stringify`` 返回字符串, 在 Python 侧解析。
    """
    raw = run_js(win, """JSON.stringify((function(){
      try {
        var pc = new RTCPeerConnection({iceServers:[]});
        var ok = pc && typeof pc.createOffer === 'function';
        try { pc.close(); } catch(e){}
        var R = window.__M3U8DL_REC;
        return {ok: ok, hooked: typeof R === 'object',
                str: String(RTCPeerConnection).indexOf('native code') >= 0};
      } catch (e) { return {ok:false, err:String(e)}; }
    })())""")
    try:
        st = json.loads(raw) if raw else {}
    except Exception:
        st = {}
    if not st:
        check(False, f"RTCPeerConnection 探测失败: {raw!r}")
        return
    check(bool(st.get("ok")), "RTCPeerConnection 仍可正常构造(代理未破坏页面)")
    check(bool(st.get("str")), "toString 已伪装成 native(不易被站点检出)")
    check(bool(st.get("hooked")), "录制状态对象已注入")

    # 轨道收集逻辑: 手动塞一条轨道, 看统计里是否计入
    n = run_js(win, """(function(){
      var R = window.__M3U8DL_REC;
      var c = document.createElement('canvas'); c.width=16; c.height=16;
      var t = c.captureStream(5).getVideoTracks()[0];
      R.tracks.video.push(t);
      return R.tracks.video.length;
    })()""")
    n = int(n or 0)
    check(n >= 1, f"webRTC 轨道池可收录轨道(现有 {n} 条)")
    # 收尾: 把这条空白 canvas 轨道撤掉, 免得后面的录制用例录到它(不重绘不产帧)
    run_js(win, "(function(){var R=window.__M3U8DL_REC;"
                "try{R.tracks.video.forEach(function(t){t.stop();});}catch(e){}"
                "R.tracks.video.length=0;return 1;})()")


def test_record_store():
    """纯 Python 侧: 追加 / 落盘 / 空录制 / 重名。"""
    store = RecordStore(WORK / "store")
    store.begin("element")
    store.add(b"\x1a\x45\xdf\xa3" + b"\x00" * 100)
    store.add(b"payload-2" * 10)
    check(store.bytes > 100 and store.chunks == 2, "分片按序累计")
    p1 = store.finish(WORK / "store_out", "rec", "video/webm")
    check(p1 is not None and p1.stat().st_size == store.bytes,
          f"落盘字节数与累计一致({p1.stat().st_size if p1 else 0})")

    store.begin("element")
    p2 = store.finish(WORK / "store_out", "rec", "video/webm")
    check(p2 is None, "空录制不产出文件")

    store.begin("element")
    store.add(b"x" * 50)
    p3 = store.finish(WORK / "store_out", "rec", "video/webm")
    check(p3 is not None and p3.name != p1.name,
          f"重名自动加序号: {p1.name if p1 else ''} -> {p3.name if p3 else ''}")

    store.begin("element")
    store.add(b"y" * 50)
    store.abort()
    check(not store.active and store.bytes == 0, "abort 丢弃未保存的录制")


def test_auto_download(win):
    """自动下载: 开关打开后解析结果自动入队, 且同一地址只入一次。"""
    win.ed_workdir.setText(str(WORK / "dl"))
    win._pump_queue = lambda: None
    ok, msg = win._toggle_auto_download(True)
    check(ok and win.auto_download, f"自动下载开关已打开: {msg}")

    before = len(win._task_order)
    infos = [
        VideoInfo(title="low", url="http://x/a.m3u8", resolution="640x360",
                  bitrate_kbps=300),
        VideoInfo(title="high", url="http://x/b.m3u8", resolution="1920x1080",
                  bitrate_kbps=2000),
    ]
    win._on_sniff_done(infos)
    added = len(win._task_order) - before
    check(added == 1, f"自动入队 1 个任务(实际 {added})")
    if added:
        tid = win._task_order[-1]
        cfg = win._tasks[tid]["cfg"]
        check(cfg.url == "http://x/b.m3u8", f"挑的是码率最高的一条: {cfg.url}")

    n2 = len(win._task_order)
    win._on_sniff_done(infos)
    check(len(win._task_order) == n2, "同一地址不会重复自动下载")

    win._toggle_auto_download(False)
    n3 = len(win._task_order)
    win._on_sniff_done([VideoInfo(title="c", url="http://x/c.m3u8")])
    check(len(win._task_order) == n3, "关闭开关后不再自动入队")


def test_data_send(win, hook_url):
    """数据发送: 载荷字段完整。"""
    win.infos = [
        VideoInfo(title="样例", url="http://x/v.m3u8", resolution="1280x720",
                  bitrate_kbps=1500, duration_text="12s", size_text="≈3 MB",
                  mime="application/vnd.apple.mpegurl",
                  headers={"Referer": "http://x/"}),
    ]
    win._web_sniff_title = "样例页"
    ok, msg = win._send_data(hook_url)
    check(ok, f"数据发送成功: {msg}")
    check(bool(_Hook.received), "服务端收到 POST")
    if _Hook.received:
        d = _Hook.received[-1]
        check(d.get("count") == 1, "载荷含 count")
        check(d.get("page") is not None, "载荷含 page")
        v = (d.get("videos") or [{}])[0]
        check(v.get("url") == "http://x/v.m3u8" and v.get("resolution") == "1280x720",
              "载荷含视频地址与清晰度")
        check(isinstance(v.get("headers"), dict) and v["headers"].get("Referer"),
              "载荷带上请求头(便于对端直接下载)")

    ok2, msg2 = win._send_data("not-a-url")
    check(not ok2, f"非法地址被拒绝: {msg2}")


def test_mobile_toggle(win):
    """模拟手机: UA 切换 + 还原。"""
    desktop = win._desktop_ua
    ok, msg = win._toggle_mobile(True)
    check(ok, f"切到移动端 UA: {msg}")
    ua = win._profile.httpUserAgent()
    check("iPhone" in ua, f"当前 UA 为 iPhone: {ua[:48]}…")
    ok2, msg2 = win._toggle_mobile(False)
    check(ok2 and win._profile.httpUserAgent() == desktop,
          f"还原桌面 UA: {msg2}")


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    test_record_store()

    app = QApplication(sys.argv)
    win = MainWindow()
    win._pump_queue = lambda: None

    srv, port = _serve(_Page)
    hook, hport = _serve(_Hook)

    # 打开测试页
    win.web.setUrl(QUrl(f"http://127.0.0.1:{port}/"))
    loaded = {"ok": False}
    win.web.page().loadFinished.connect(lambda ok: loaded.__setitem__("ok", ok))
    deadline = time.monotonic() + 20
    while not loaded["ok"] and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.05)
    check(loaded["ok"], "测试页已加载")

    _wait(800)                     # 让注入脚本与 canvas 动画跑起来
    test_mime_support(win)
    test_rtc_proxy_alive(win)
    test_record_canvas_stream(win)
    test_auto_download(win)
    test_data_send(win, f"http://127.0.0.1:{hport}/hook")
    test_mobile_toggle(win)

    srv.shutdown()
    hook.shutdown()

    print()
    if FAIL:
        print(f"RECORDER TEST FAILED: {len(FAIL)} 项失败")
        for f in FAIL:
            print("  -", f)
        return 1
    print(f"RECORDER TEST OK: {len(PASS)} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
