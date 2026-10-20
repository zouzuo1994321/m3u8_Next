"""下载引擎集成测试: 本地 ffmpeg 生成 HLS 测试流, 验证 VOD/LIVE/AES/fMP4 全链路。

覆盖:
- TS VOD + 二进制合并(输出 .ts, ffmpeg 校验可解码)
- TS VOD + ffmpeg 合并(输出 .mp4)
- fMP4 VOD(init + 分片二进制拼接输出 .mp4)
- AES-128 加密 VOD(自动解密)
- 直播流录制(滚动窗口边下边存)
"""
import http.server
import secrets
import socketserver
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3u8dl.downloader import DownloadConfig, M3U8Downloader

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".test_work"
FFMPEG = ROOT / "assets" / "ffmpeg.exe"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class LocalServer:
    def __init__(self, directory: Path):
        self.httpd = socketserver.TCPServer(
            ("127.0.0.1", 0),
            lambda *a, **kw: _QuietHandler(*a, directory=str(directory), **kw))
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


@unittest.skipIf(not FFMPEG.exists(), "assets/ffmpeg.exe 不存在")
class TestDownloader(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        WORK.mkdir(exist_ok=True)
        cls.src = WORK / "src.mp4"
        if not cls.src.exists():
            # -g 15: 每 15 帧一个关键帧(15fps -> 约 1s), 保证 -c copy 能按 2s 切片
            _ff(["-f", "lavfi", "-i", "testsrc=duration=12:size=320x240:rate=15",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
                 "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
                 "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-shortest", str(cls.src)])
        # TS VOD
        cls.ts_dir = WORK / "ts"
        cls.ts_dir.mkdir(exist_ok=True)
        _ff(["-i", str(cls.src), "-c", "copy", "-f", "hls",
             "-hls_time", "2", "-hls_list_size", "0",
             "-hls_segment_filename", str(cls.ts_dir / "seg%d.ts"),
             str(cls.ts_dir / "index.m3u8")])
        # fMP4 VOD(cwd 设为输出目录, 保证 m3u8 内为相对地址)
        cls.fmp4_dir = WORK / "fmp4"
        cls.fmp4_dir.mkdir(exist_ok=True)
        subprocess.run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(cls.src), "-c", "copy", "-f", "hls",
                        "-hls_segment_type", "fmp4", "-hls_time", "2",
                        "-hls_list_size", "0",
                        "-hls_fmp4_init_filename", "init.mp4",
                        "-hls_segment_filename", "frag%d.m4s",
                        "index.m3u8"],
                       check=True, capture_output=True, cwd=str(cls.fmp4_dir))
        # AES-128 VOD
        cls.aes_dir = WORK / "aes"
        cls.aes_dir.mkdir(exist_ok=True)
        key = secrets.token_bytes(16)
        (cls.aes_dir / "key.bin").write_bytes(key)
        (cls.aes_dir / "key_info").write_text(
            f"key.bin\n{(cls.aes_dir / 'key.bin').as_posix()}\n")
        _ff(["-i", str(cls.src), "-c", "copy", "-f", "hls",
             "-hls_time", "2", "-hls_list_size", "0",
             "-hls_key_info_file", str(cls.aes_dir / "key_info"),
             "-hls_segment_filename", str(cls.aes_dir / "seg%d.ts"),
             str(cls.aes_dir / "index.m3u8")])

    def _run_download(self, url, save_name, **overrides):
        out_dir = WORK / "out" / save_name
        out_dir.mkdir(parents=True, exist_ok=True)
        logs = []
        cfg = DownloadConfig(url=url, save_dir=str(out_dir), save_name=save_name,
                             threads=8, retry=3, timeout=10, use_proxy=False,
                             **overrides)
        dl = M3U8Downloader(cfg, on_log=logs.append,
                            on_progress=lambda d: None, on_state=lambda s: None,
                            ffmpeg_path=FFMPEG)
        status = dl.run()
        return status, out_dir, logs

    def test_ts_vod_binary_merge(self):
        with LocalServer(self.ts_dir) as srv:
            status, out_dir, logs = self._run_download(
                srv.url("index.m3u8"), "ts_vod", binary_merge=True)
        self.assertEqual(status, "done")
        out = out_dir / "ts_vod.ts"
        self.assertTrue(out.exists() and out.stat().st_size > 10000, "输出文件过小")
        self.assertTrue(_decode_ok(out), "合并后的 ts 无法解码")
        # 临时分片应已清理
        self.assertFalse((out_dir / ".m3u8dl_ts_vod").exists())

    def test_ts_vod_ffmpeg_merge(self):
        with LocalServer(self.ts_dir) as srv:
            status, out_dir, logs = self._run_download(
                srv.url("index.m3u8"), "ts_ff", binary_merge=False)
        self.assertEqual(status, "done")
        out = out_dir / "ts_ff.mp4"
        self.assertTrue(out.exists(), "未生成 mp4")
        self.assertTrue(_decode_ok(out), "合并后的 mp4 无法解码")

    def test_fmp4_vod(self):
        with LocalServer(self.fmp4_dir) as srv:
            status, out_dir, logs = self._run_download(
                srv.url("index.m3u8"), "fmp4_vod")
        self.assertEqual(status, "done")
        out = out_dir / "fmp4_vod.mp4"
        self.assertTrue(out.exists(), "未生成 mp4")
        self.assertTrue(_decode_ok(out), "fMP4 拼接结果无法解码")

    def test_aes_vod(self):
        with LocalServer(self.aes_dir) as srv:
            status, out_dir, logs = self._run_download(
                srv.url("index.m3u8"), "aes_vod")
        self.assertEqual(status, "done")
        out = out_dir / "aes_vod.ts"
        self.assertTrue(out.exists(), "未生成 ts")
        self.assertTrue(_decode_ok(out), "AES 解密结果无法解码")
        self.assertTrue(any("AES-128" in l for l in logs), "日志缺少 AES 提示")

    def test_range_select(self):
        with LocalServer(self.ts_dir) as srv:
            # 先下完整版作对照
            _, full_dir, _ = self._run_download(srv.url("index.m3u8"), "range_full")
            full_size = (full_dir / "range_full.ts").stat().st_size
            # 再下 4-8 秒范围
            status, out_dir, logs = self._run_download(
                srv.url("index.m3u8"), "range_vod",
                range_start="00:00:04", range_end="00:00:08")
        self.assertEqual(status, "done")
        out = out_dir / "range_vod.ts"
        self.assertTrue(out.exists())
        self.assertTrue(any("范围选择" in l for l in logs), "日志缺少范围选择提示")
        self.assertLess(out.stat().st_size, full_size * 0.6, "范围选择未生效")

    def test_live_record(self):
        live_dir = WORK / "live"
        live_dir.mkdir(exist_ok=True)
        proc = subprocess.Popen([
            str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
            "-re", "-stream_loop", "-1", "-i", str(self.src), "-c", "copy",
            "-f", "hls", "-hls_time", "2", "-hls_list_size", "3",
            "-hls_segment_filename", str(live_dir / "seg%d.ts"),
            str(live_dir / "live.m3u8")])
        try:
            for _ in range(50):
                if (live_dir / "live.m3u8").exists():
                    break
                time.sleep(0.2)
            time.sleep(3)
            with LocalServer(live_dir) as srv:
                out_dir = WORK / "out" / "live"
                out_dir.mkdir(parents=True, exist_ok=True)
                logs = []
                cfg = DownloadConfig(url=srv.url("live.m3u8"), save_dir=str(out_dir),
                                     save_name="live", threads=4, retry=3,
                                     use_proxy=False)
                dl = M3U8Downloader(cfg, on_log=logs.append,
                                    on_progress=lambda d: None,
                                    on_state=lambda s: None, ffmpeg_path=FFMPEG)
                t = threading.Thread(target=dl.run, daemon=True)
                t.start()
                time.sleep(8)
                dl.stop()
                t.join(timeout=15)
            out = out_dir / "live.ts"
            self.assertTrue(out.exists() and out.stat().st_size > 10000,
                            "直播录制文件过小")
            self.assertTrue(_decode_ok(out), "直播录制文件无法解码")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    unittest.main(verbosity=2)
