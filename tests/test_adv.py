"""深度搜索 / 缓存捕捉 / 更多功能 的集成验证。

三块能力各自有单测(test_deepsearch / test_capture / test_fragtools), 这里只
验「接进主窗口之后」的那一层:

1. **内存清单可直接下载**: ``DownloadConfig.playlist_text`` 有值时, 下载器不再
   请求 url。测试把 url 指向一个必然 404 的路径, 只有不请求才可能成功。
2. **深度搜索结果落地**: ``_apply_deep_result`` 把 m3u8 原文变成可下载条目, 分片
   地址绝对化, 并留下疑似密钥供「更多功能」验证。
3. **更多功能落到下载参数**: 面板里的选项经 ``_on_download`` 写进 DownloadConfig。

用法: QT_QPA_PLATFORM=offscreen python tests/test_adv.py
"""
import http.server
import os
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 预览必须走 QtMultimedia 的 FFmpeg 后端(须在 QApplication 之前)
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from PySide6.QtCore import QEventLoop, Qt, QTimer, QUrl
from PySide6.QtWidgets import QApplication

from m3u8dl import deepsearch
from m3u8dl.downloader import DownloadConfig, M3U8Downloader
from m3u8dl.playlist import MediaPlaylist, parse_m3u8
from m3u8dl.ui.main_window import MainWindow

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".test_work" / "adv"
FFMPEG = ROOT / "assets" / "ffmpeg.exe"

PASS, FAIL = [], []


def check(ok, msg):
    (PASS if ok else FAIL).append(msg)
    print(f"[{'PASS' if ok else 'FAIL'}] {msg}")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class _Server:
    def __init__(self, directory: Path):
        self.httpd = socketserver.TCPServer(
            ("127.0.0.1", 0),
            lambda *a, **kw: _Quiet(*a, directory=str(directory), **kw))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path}"


def _ff(args):
    subprocess.run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error"] + args,
                   check=True, capture_output=True)


def _decode_ok(path: Path) -> bool:
    r = subprocess.run([str(FFMPEG), "-v", "error", "-i", str(path), "-f", "null", "-"],
                       capture_output=True)
    return r.returncode == 0


# ------------------------------------------------------------------ 1. 内存清单
def test_playlist_text(segs_dir: Path):
    """playlist_text 生效时完全不请求 url。"""
    segs = sorted(segs_dir.glob("seg*.ts"))
    with _Server(segs_dir) as srv:
        # 相对地址改写成绝对地址(模拟深度搜索从内存里捞到的原文)
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2"]
        for s in segs:
            lines.append("#EXTINF:2.000,")
            lines.append(srv.url(s.name))
        lines.append("#EXT-X-ENDLIST")
        text = "\n".join(lines) + "\n"

        out_dir = WORK / "out_text"
        out_dir.mkdir(parents=True, exist_ok=True)
        logs = []
        cfg = DownloadConfig(
            # 故意指向不存在的路径: 只要下载器去请求就必然失败
            url=srv.url("__missing__/never.m3u8"),
            save_dir=str(out_dir), save_name="memlist",
            threads=4, retry=1, timeout=5, use_proxy=False,
            binary_merge=True, playlist_text=text)
        status = M3U8Downloader(
            cfg, on_log=logs.append, on_progress=lambda d: None,
            on_state=lambda s: None, ffmpeg_path=FFMPEG).run()

    out = out_dir / "memlist.ts"
    check(status == "done", f"playlist_text 下载完成(status={status})")
    check(out.exists() and out.stat().st_size > 10000,
          f"输出文件已生成({out.stat().st_size if out.exists() else 0} 字节)")
    check(out.exists() and _decode_ok(out), "内存清单合并结果可解码")
    check(any("不请求网络" in m for m in logs), "日志确认走的是内存清单通路")
    check(not any("请求失败" in m for m in logs),
          "全程没有发生播放列表请求(否则会因 404 报请求失败)")


# ------------------------------------------------- 2. 深度搜索结果 -> 视频条目
def test_deep_result_to_tree(win: MainWindow, base: str):
    seg_text = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n"
                "#EXTINF:2.0,\n"
                f"{base}/seg0.ts\n"
                "#EXTINF:2.0,\n"
                f"{base}/seg1.ts\n"
                "#EXT-X-ENDLIST\n")
    payload = ('{"urls":["' + base + '/stream/main.m3u8"],'
               '"keys":[{"k":"00112233445566778899aabbccddeeff","t":"hex32",'
               '"from":"fromCharCode"}],'
               '"texts":[{"t":' + _json_str(seg_text) + ',"from":"fromCharCode"}]}')

    before = win.tree.topLevelItemCount()
    win._apply_deep_result(payload)

    added = [win.tree.topLevelItem(i).data(0, Qt.UserRole)
             for i in range(before, win.tree.topLevelItemCount())]
    infos = [a for a in added if a is not None]
    check(win.tree.topLevelItemCount() > before,
          f"深度搜索结果已进列表(+{win.tree.topLevelItemCount() - before} 条)")

    mem = [i for i in infos if getattr(i, "playlist_text", "")]
    check(len(mem) == 1, f"m3u8 原文转成 1 个可下载条目(实际 {len(mem)})")
    if mem:
        info = mem[0]
        pl = parse_m3u8(info.playlist_text, info.source or info.url)
        check(isinstance(pl, MediaPlaylist) and len(pl.segments) == 2,
              "条目携带的原文可解析出 2 个分片")
        check(str(pl.segments[0].url).startswith("http://"),
              "分片地址已绝对化")
        check(info.size_text.endswith("分片"), f"条目显示分片数: {info.size_text}")

    keys = deepsearch.key_candidates(win._deep_keys)
    check(len(keys) == 1 and keys[0].hex() == "00112233445566778899aabbccddeeff",
          "疑似密钥已规范化成 16 字节")


def _json_str(s: str) -> str:
    import json

    return json.dumps(s)


# --------------------------------------- 3. 内存清单可直接预览(不需真实地址)
def test_memory_preview(win: MainWindow, base: str):
    """回归: 本地文件喂 FFmpeg 会因为协议白名单失败, 必须走本地 http。"""
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

    text = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n"
            "#EXTINF:2.0,\n" f"{base}/seg0.ts\n"
            "#EXTINF:2.0,\n" f"{base}/seg1.ts\n"
            "#EXT-X-ENDLIST\n")
    url = win.preview.memory_url(text, {"Referer": base + "/"}, base)
    check("/mem/" in url, "内存清单已挂到本地预览服务")

    player = QMediaPlayer()
    sink = QVideoSink()
    player.setVideoSink(sink)
    st = {"frames": 0, "err": ""}
    sink.videoFrameChanged.connect(
        lambda f: st.__setitem__("frames", st["frames"] + 1) if f.isValid() else None)
    player.errorOccurred.connect(lambda e, s: st.__setitem__("err", s))

    player.setSource(QUrl(url))
    player.play()
    loop = QEventLoop()
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    player.stop()
    check(st["frames"] > 0,
          f"内存清单可直接预览(解码 {st['frames']} 帧, 错误: {st['err'] or '无'})")


# ------------------------------------------- 4. 更多功能选项 -> DownloadConfig
def test_adv_to_config(win: MainWindow):
    from m3u8dl.sniffer import VideoInfo

    info = VideoInfo(title="adv", url="http://x/a.m3u8")
    win.current_info = info
    win.ed_workdir.setText(str(WORK / "out_adv"))
    win.ed_save.setText("advcfg")
    win.adv = {
        "strip_image": True, "skip_decrypt": True, "audio_only": True,
        "extra_query": "token=abc", "selected": [0, 2], "regex": "",
    }
    win._pump_queue = lambda: None
    win._on_download()

    tid = win._task_order[-1]
    cfg = win._tasks[tid]["cfg"]
    check(cfg.strip_image and cfg.skip_decrypt and cfg.audio_only,
          "剥离伪装头/跳过解密/仅音频 三个开关已落到 DownloadConfig")
    check(cfg.extra_query == "token=abc", "分片参数已落到 DownloadConfig")
    check(cfg.selected == [0, 2], "分片筛选下标已落到 DownloadConfig")


def main():
    app = QApplication(sys.argv)
    WORK.mkdir(parents=True, exist_ok=True)
    win = MainWindow()
    win._pump_queue = lambda: None

    # --- 造一段 TS HLS 素材 ---
    need_ff = FFMPEG.exists()
    if need_ff:
        segs = WORK / "segs"
        segs.mkdir(exist_ok=True)
        if not list(segs.glob("seg*.ts")):
            src = WORK / "src.mp4"
            if not src.exists():
                _ff(["-f", "lavfi", "-i",
                     "testsrc=duration=8:size=320x240:rate=15",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
                     "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
                     "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)])
            _ff(["-i", str(src), "-c", "copy", "-f", "hls",
                 "-hls_time", "2", "-hls_list_size", "0",
                 "-hls_segment_filename", str(segs / "seg%d.ts"),
                 str(segs / "index.m3u8")])
        test_playlist_text(segs)
    else:
        print("[SKIP] assets/ffmpeg.exe 不存在, 跳过内存清单下载")

    with _Server(WORK) as srv:
        base = srv.url("segs")
        test_deep_result_to_tree(win, base)
        test_memory_preview(win, base)
    test_adv_to_config(win)

    print()
    if FAIL:
        print(f"ADV TEST FAILED: {len(FAIL)} 项失败")
        for f in FAIL:
            print("  -", f)
        return 1
    print(f"ADV TEST OK: {len(PASS)} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
