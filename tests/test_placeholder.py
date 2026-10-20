"""占位流(taunt/promo)与 master 循环的回归测试。

来历
----
实测某网盘型源站(uqload)在请求**未获授权**时(缺 Cookie / token 过期),
不返回 403, 而是 302 到一张"钓鱼清单"::

    GET /hls2/03/05948/xxx/index.m3u8
    302 -> https://<host>/taunt/promo/master.m3u8

    #EXTM3U
    #EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=1280x720
    index.m3u8              <- 相对地址, 且与请求原名相同

旧代码有两处会在这里翻车:

1. ``parse_m3u8(text, 请求前地址)`` —— base 用错了。变体 ``index.m3u8`` 被解析
   回**请求前地址本身**, 于是"master → 变体 → master"死循环, 最后抛出完全
   误导的「无法解析 m3u8 播放列表」。
2. 占位清单是合法 m3u8, 嗅探把它当成真视频收录(日志里那个 1280x720 / 500kbps
   假条目就是这么来的), 用户点下载必然失败。

本测试用本地服务复刻这两个行为, 验证:
- 重定向后的相对变体按**最终地址**解析(而不是原地址)
- 占位流被识别并跳过/明确报错, 不再死循环
- 真正的多层 master 仍能正常展开、正常下载

用法: python tests/test_placeholder.py
"""
import http.server
import socketserver
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3u8dl.downloader import DownloadConfig, M3U8Downloader
from m3u8dl.playlist import is_placeholder_url
from m3u8dl.sniffer import Sniffer

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / ".test_work" / "placeholder"
FFMPEG = ROOT / "assets" / "ffmpeg.exe"

PASS, FAIL = [], []


def check(ok, msg):
    (PASS if ok else FAIL).append(msg)
    print(f"[{'PASS' if ok else 'FAIL'}] {msg}")


# ---------------------------------------------------------------- 模拟源站
#: 占位清单: 与实测响应逐字一致(变体是相对地址 index.m3u8)
PROMO_MASTER = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=1280x720,'
    'CODECS="avc1.4d401f,mp4a.40.2"\n'
    "index.m3u8\n"
)

#: 真正的自引用 master: 变体地址与清单自身同名, 解析后指回自己
SELF_MASTER = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=1280x720\n'
    "master.m3u8\n"
)

#: 真正的多层 master: 变体是相对地址, 必须相对**重定向后**的目录解析
LEGIT_MASTER = (
    "#EXTM3U\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n'
    "v360.m3u8\n"
    '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n'
    "v720.m3u8\n"
)


class _Handler(http.server.BaseHTTPRequestHandler):
    """按路径返回不同内容, 用于复刻重定向与占位流。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # ---- 工具
    def _send(self, code, body=b"", ctype="application/vnd.apple.mpegurl",
              location=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _file(self, rel):
        p = WORK / "www" / rel
        if p.exists():
            self._send(200, p.read_bytes(),
                       "video/mp2t" if p.suffix == ".ts" else "application/vnd.apple.mpegurl")
        else:
            self._send(404, b"not found", "text/plain")

    # ---- 路由
    def do_GET(self):
        path = self.path.split("?")[0]
        base = f"http://127.0.0.1:{self.server.server_address[1]}"

        # 1) 未授权请求 -> 302 到占位清单(真实源站行为)
        if path == "/hls/unauth/index.m3u8":
            self._send(302, location=base + "/taunt/promo/master.m3u8")
            return
        # 占位清单本体: 变体 index.m3u8 相对它自己的目录
        if path == "/taunt/promo/master.m3u8":
            self._send(200, PROMO_MASTER)
            return
        # 占位流就算被跟着解析, 也只是再回到占位清单(死循环现场)
        if path == "/taunt/promo/index.m3u8":
            self._send(200, PROMO_MASTER)
            return

        # 2) 合法重定向: /a/master.m3u8 -> /deep/real/master.m3u8
        #    变体是相对地址 v720.m3u8, 只有用最终地址做 base 才正确
        if path == "/a/master.m3u8":
            self._send(302, location=base + "/deep/real/master.m3u8")
            return
        if path == "/deep/real/master.m3u8":
            self._send(200, LEGIT_MASTER)
            return
        if path in ("/deep/real/v360.m3u8", "/deep/real/v720.m3u8"):
            name = path.rsplit("/", 1)[-1]
            self._send(200, self._media_pl(name))
            return

        # 3) 自引用 master(不靠重定向, 变体直接指回自己)
        if path == "/self/master.m3u8":
            self._send(200, SELF_MASTER)
            return

        # 4) 正常单层清单
        if path == "/ok/index.m3u8":
            self._send(200, self._media_pl("v720.m3u8"))
            return
        if path.startswith("/ok/") or path.startswith("/deep/real/"):
            self._file(path.lstrip("/").replace("deep/real/", "ok/"))
            return
        self._send(404)

    @staticmethod
    def _media_pl(seg: str) -> str:
        return ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n"
                "#EXT-X-PLAYLIST-TYPE:VOD\n"
                f"#EXTINF:2.000,\n{seg.replace('.m3u8', '')}_0.ts\n"
                f"#EXTINF:2.000,\n{seg.replace('.m3u8', '')}_1.ts\n"
                "#EXT-X-ENDLIST\n")


class Server:
    def __init__(self):
        self.httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


def _ff(args):
    subprocess.run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error"] + args,
                   check=True, capture_output=True)


def prepare_media():
    """生成 TS 分片素材, 供"正常链路"用例下载。"""
    www = WORK / "www" / "ok"
    www.mkdir(parents=True, exist_ok=True)
    if (www / "v720_1.ts").exists() and (www / "v360_1.ts").exists():
        return
    src = WORK / "src.mp4"
    if not src.exists():
        _ff(["-f", "lavfi", "-i", "testsrc=duration=4:size=320x180:rate=15",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
             "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)])
    for tag in ("v360", "v720"):
        _ff(["-i", str(src), "-c", "copy", "-f", "hls",
             "-hls_time", "2", "-hls_list_size", "0",
             "-hls_segment_filename", str(www / f"{tag}_%d.ts"),
             "-start_number", "0", str(www / f"{tag}.m3u8")])


# ---------------------------------------------------------------- 用例
def test_placeholder_helper():
    check(is_placeholder_url("https://x/taunt/promo/master.m3u8"),
          "占位流识别: taunt/promo 命中")
    check(not is_placeholder_url("https://x/hls/03/05948/abc/index-v1-a1.m3u8"),
          "占位流识别: 正常清单不误判")
    check(not is_placeholder_url("https://x/production/master.m3u8"),
          "占位流识别: production 不被误判为 promo")


def test_sniffer_skips_placeholder(srv: Server):
    """嗅探: 被 302 到占位流的地址不产出假视频。"""
    logs = []
    s = Sniffer(timeout=8, use_proxy=False, log=logs.append)
    infos = s.sniff_urls([srv.url("/hls/unauth/index.m3u8")],
                         page_url=srv.url("/page"), page_title="t")
    check(infos == [], f"占位流未被收录为视频(实际 {len(infos)} 条)")
    check(any("重定向" in m for m in logs), "日志提示了地址重定向")
    check(any("占位" in m for m in logs), "日志说明了这是占位/广告流")


def test_downloader_rejects_placeholder(srv: Server):
    """下载: 占位流要给出准确原因, 且不能死循环。"""
    out = WORK / "out_ph"
    out.mkdir(parents=True, exist_ok=True)
    logs = []
    cfg = DownloadConfig(url=srv.url("/hls/unauth/index.m3u8"),
                         save_dir=str(out), save_name="ph",
                         threads=2, retry=1, timeout=8, use_proxy=False)
    status = M3U8Downloader(cfg, on_log=logs.append, on_progress=lambda d: None,
                            on_state=lambda s: None, ffmpeg_path=FFMPEG).run()
    joined = "\n".join(logs)
    check(status == "failed", "占位流下载被判定为失败")
    check("占位流" in joined, "报错明确指出「占位流」而非「无法解析」")
    check("taunt/promo" in joined, "报错带上了占位流的实际地址")
    check(joined.count("读取到大师列表") <= 1,
          f"没有反复展开 master(实际 {joined.count('读取到大师列表')} 次)")


def test_self_reference_master(srv: Server):
    """不靠重定向的自引用 master 也要能识别(而非转圈/误导报错)。"""
    out = WORK / "out_self"
    out.mkdir(parents=True, exist_ok=True)
    logs = []
    cfg = DownloadConfig(url=srv.url("/self/master.m3u8"), save_dir=str(out),
                         save_name="self", threads=2, retry=1, timeout=8,
                         use_proxy=False)
    status = M3U8Downloader(cfg, on_log=logs.append, on_progress=lambda d: None,
                            on_state=lambda s: None, ffmpeg_path=FFMPEG).run()
    joined = "\n".join(logs)
    check(status == "failed", "自引用 master 下载失败(不会死循环)")
    check("变体指向自身" in joined, "报错说明「变体指向自身」")


def test_redirect_base_is_final_url(srv: Server):
    """核心回归: 重定向后, 相对变体必须相对最终地址解析。"""
    logs = []
    s = Sniffer(timeout=8, use_proxy=False, log=logs.append)
    infos = s.sniff_urls([srv.url("/a/master.m3u8")],
                         page_url=srv.url("/page"), page_title="t")
    urls = [i.url for i in infos]
    check(len(infos) == 2, f"master 展开出 2 个变体(实际 {len(infos)})")
    check(all("/deep/real/" in u for u in urls),
          "变体地址已按最终目录解析(不是 /a/ 下)")


def test_legit_multi_level_download(srv: Server):
    """正常的多层 master 仍能下载成功(修复不能误伤正路)。"""
    out = WORK / "out_ok"
    out.mkdir(parents=True, exist_ok=True)
    logs = []
    cfg = DownloadConfig(url=srv.url("/a/master.m3u8"), save_dir=str(out),
                         save_name="ok", threads=4, retry=2, timeout=8,
                         use_proxy=False, binary_merge=True)
    status = M3U8Downloader(cfg, on_log=logs.append, on_progress=lambda d: None,
                            on_state=lambda s: None, ffmpeg_path=FFMPEG).run()
    joined = "\n".join(logs)
    check(status == "done", f"经重定向的多层 master 下载完成(status={status})")
    check(any("地址已重定向" in m for m in logs), "日志记录了重定向")
    out_file = out / "ok.ts"
    check(out_file.exists() and out_file.stat().st_size > 5000,
          f"输出文件已生成({out_file.stat().st_size if out_file.exists() else 0} 字节)")


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    if not FFMPEG.exists():
        print("[SKIP] assets/ffmpeg.exe 不存在")
        return 1
    prepare_media()
    test_placeholder_helper()
    with Server() as srv:
        test_sniffer_skips_placeholder(srv)
        test_downloader_rejects_placeholder(srv)
        test_self_reference_master(srv)
        test_redirect_base_is_final_url(srv)
        test_legit_multi_level_download(srv)

    print()
    if FAIL:
        print(f"PLACEHOLDER TEST FAILED: {len(FAIL)} 项失败")
        for f in FAIL:
            print("  -", f)
        return 1
    print(f"PLACEHOLDER TEST OK: {len(PASS)} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
