"""集成测试: QMediaPlayer 经本地预览代理播放 H.264 HLS 流。

验证链路: 源站 HTTP 服务 -> PreviewServer 代理(重写 m3u8/代填请求头)
        -> QMediaPlayer(FFmpeg 后端) 解码出视频帧。
这是替代 QtWebEngine + hls.js 预览方案的关键前提。

用法: python tests/test_preview_player.py
"""
import os
import subprocess
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

ROOT = Path(__file__).resolve().parent.parent
FFMPEG = ROOT / "assets" / "ffmpeg.exe"


def make_hls(dirpath: Path) -> Path:
    m3u8 = dirpath / "stream.m3u8"
    cmd = [
        str(FFMPEG), "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=480x270:rate=25:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
        "-hls_segment_filename", str(dirpath / "s%03d.ts"),
        str(m3u8),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return m3u8


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def main():
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

    from m3u8dl.preview import PreviewServer

    tmp = Path(tempfile.mkdtemp(prefix="m3u8dl_pvtest_"))
    make_hls(tmp)

    # 起一个"源站"
    handler = partial(QuietHandler, directory=str(tmp))
    origin = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    origin.daemon_threads = True
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    origin_port = origin.server_address[1]

    preview = PreviewServer(ROOT / "assets" / "hls.min.js")
    preview.start()

    stream_url = f"http://127.0.0.1:{origin_port}/stream.m3u8"
    proxy = preview.proxy_url(stream_url, {"Referer": f"http://127.0.0.1:{origin_port}/"})
    print(f"源站: {stream_url}")
    print(f"代理: {proxy}")

    app = QApplication(sys.argv)
    player = QMediaPlayer()
    sink = QVideoSink()
    player.setVideoSink(sink)

    st = {"frames": 0, "err": ""}
    sink.videoFrameChanged.connect(
        lambda f: st.__setitem__("frames", st["frames"] + 1) if f.isValid() else None)
    player.errorOccurred.connect(
        lambda e, s: st.__setitem__("err", f"{e}: {s}"))

    player.setSource(QUrl(proxy))
    player.play()

    def finish():
        print()
        print("解码帧数:", st["frames"])
        print("错误:", st["err"] or "(无)")
        ok = st["frames"] > 0
        if ok:
            print("[PASS] QMediaPlayer 经本地代理成功解码 H.264 HLS")
        else:
            print("[FAIL] 未能解码")
        player.stop()
        preview.stop()
        origin.shutdown()
        print("\nPREVIEW PLAYER TEST", "OK" if ok else "FAILED")
        app.exit(0 if ok else 1)

    QTimer.singleShot(7000, finish)
    rc = app.exec()
    return rc


if __name__ == "__main__":
    sys.exit(main())
