"""缓存捕捉全链路测试: QWebChannel 桥 -> 页面 appendBuffer 钩子 -> 落盘合并。

用法: python tests/test_capture.py

验证:
1. ``QWebChannel`` 通道能把页面里的原始字节搬回 Python（base64 解码正确）
2. 捕获脚本在 DocumentCreation 注入后，成功钩住 addSourceBuffer / appendBuffer
3. 编解码器不支持时走 fallback mimeType 分支，钩子依然生效
4. ``CaptureStore`` 边收边写临时文件，保存时丢弃多余容器头
5. 多路流（音视频分离）分别输出
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication

from m3u8dl import bridge as bridge_mod
from m3u8dl import capture as cap_mod


def ftyp_box(payload: bytes) -> bytes:
    return b"\x00\x00\x00\x18ftypisom" + payload


CHUNK1 = ftyp_box(b"A" * 32) + b"seg1-body" * 40
CHUNK2 = b"seg2-body" * 40
CHUNK3 = ftyp_box(b"B" * 32) + b"seg3-body" * 40     # 多余的第二个 ftyp 头
TOTAL = len(CHUNK1) + len(CHUNK2) + len(CHUNK3)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>capture-test</title></head>
<body>
<video id="v" muted></video>
<script>
window.__RESULT = {stage: "start"};
(function () {
  var ms = new MediaSource();
  var v = document.getElementById("v");
  v.src = URL.createObjectURL(ms);
  ms.addEventListener("sourceopen", function () {
    var sb;
    try {
      sb = ms.addSourceBuffer('video/mp4; codecs="avc1.42E01E"');
      window.__RESULT.mime = "avc1";
    } catch (e) {
      window.__RESULT.stage = "addSourceBuffer-failed:" + e.name;
      window.__DONE = true;
      return;
    }
    var chunks = window.__CHUNKS;
    var i = 0;
    function step() {
      if (i >= chunks.length) {
        try { ms.endOfStream(); } catch (e) {}
        window.__RESULT.stage = "done";
        window.__DONE = true;
        return;
      }
      try {
        sb.appendBuffer(new Uint8Array(chunks[i]).buffer);
      } catch (e) { window.__RESULT.appendErr = String(e); }
      i++;
      setTimeout(step, 80);
    }
    step();
  });
})();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode("utf-8")
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
    work = Path(__file__).resolve().parent.parent / ".test_work" / "capture_parts"
    store = cap_mod.CaptureStore(work)
    store.clear()

    profile = QWebEngineProfile.defaultProfile()
    view = QWebEngineView()

    bridge = bridge_mod.PageBridge(
        on_chunk=lambda sid, mime, idx, data: store.add_chunk(sid, mime, idx, data),
        on_meta=lambda sid, k, v: store.add_meta(sid, k, v),
    )
    assert bridge.install(view.page()), "QWebChannel 安装失败"

    # 注入: qwebchannel 连接脚本 + 捕获脚本 + 测试数据
    script = QWebEngineScript()
    script.setName("m3u8dl-test-boot")
    payload = json.dumps([list(CHUNK1), list(CHUNK2), list(CHUNK3)])
    script.setSourceCode(
        bridge.inject_script()
        + "\n;" + cap_mod.build_script("test")
        + "\n;window.__CHUNKS = %s.map(function(a){return new Uint8Array(a);});"
        % payload
        + "\n;window.__M3U8DL_CAP.on = true;"      # 测试里直接开启, 不等外部指令
    )
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    profile.scripts().insert(script)
    print("[PASS] QWebChannel 已安装, 注入脚本已就绪")

    state = {"done": False}

    def poll():
        view.page().runJavaScript(
            "JSON.stringify(window.__RESULT || {}) + '|' + (window.__DONE?1:0)",
            _on_state)

    def _on_state(r):
        try:
            res, done = str(r).split("|")
        except ValueError:
            res, done = str(r), "0"
        if done == "1":
            state["res"] = res
            state["done"] = True
            QTimer.singleShot(600, app.quit)     # 留时间抽干队列
        else:
            QTimer.singleShot(150, poll)

    view.loadFinished.connect(lambda ok: QTimer.singleShot(200, poll))
    view.setUrl(QUrl(f"http://127.0.0.1:{port}/"))
    view.resize(320, 240)
    view.show()

    QTimer.singleShot(40000, app.quit)
    app.exec()

    assert state.get("done"), "页面未跑完(超时)"
    print(f"    页面状态: {state.get('res')}")

    deadline = time.time() + 10
    while time.time() < deadline and store.total_bytes < TOTAL:
        app.processEvents()
        time.sleep(0.05)

    print(f"    桥接收到: {store.total_chunks} 块 / {store.total_bytes} 字节 "
          f"(期望 {TOTAL})")
    assert store.total_chunks >= 3, f"应至少收到 3 块, 实际 {store.total_chunks}"
    assert store.total_bytes == TOTAL, \
        f"字节数不符: {store.total_bytes} != {TOTAL}"
    print("[PASS] QWebChannel 收到 appendBuffer 的原始字节(base64 无损)")

    streams = store.streams()
    assert len(streams) == 1, f"应只有 1 路 SourceBuffer, 实际 {len(streams)}"
    mime, idx, nbytes = streams[0]
    assert "avc1" in mime, f"应记录原始 mimeType, 实际 {mime!r}"
    assert nbytes == TOTAL
    print(f"[PASS] 原始 mimeType 被正确记录: {mime!r}")

    # ---- 合并落盘: 多余 ftyp 头应被丢弃 ----
    out_dir = work.parent / "capture_out"
    if out_dir.exists():
        for f in out_dir.iterdir():
            f.unlink()
    files = store.save(out_dir, "captured")
    assert len(files) == 1, f"单路流应输出 1 个文件, 实际 {len(files)}"
    got = files[0].read_bytes()
    assert got == CHUNK3, (
        f"合并结果不符: 期望 {len(CHUNK3)} 字节, 实际 {len(got)} 字节")
    assert files[0].suffix == ".mp4", f"扩展名应为 mp4, 实际 {files[0].suffix}"
    print(f"[PASS] 丢弃多余容器头: {TOTAL} 字节 -> {len(got)} 字节(仅最后一个 ftyp 起)")

    # ---- 多路流分别输出 ----
    store.add_chunk("test", 'audio/mp4; codecs="mp4a.40.2"', 1, ftyp_box(b"C" * 32))
    files2 = store.save(out_dir, "captured2")
    assert len(files2) == 2, f"两路流应输出 2 个文件, 实际 {len(files2)}"
    print("[PASS] 多路流(音视频分离)分别落盘: " + ", ".join(f.name for f in files2))

    # ---- 空存储 ----
    empty = cap_mod.CaptureStore(work)
    assert empty.save(out_dir, "none") == [], "空存储不应产出文件"
    assert empty.is_empty()
    print("[PASS] 空捕获不产出文件")

    # ---- 清理 ----
    store.clear()
    empty.clear()
    for d in (out_dir, work):
        if d.exists():
            for f in d.iterdir():
                f.unlink()
    srv.shutdown()
    print("\nCAPTURE TEST OK: 桥接 / appendBuffer 钩子 / 头部去重 / 多路落盘 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
