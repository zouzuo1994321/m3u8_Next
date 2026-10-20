"""Referer 策略回归测试 —— uqload 型"Referer 必须是自身域"的站点。

来历（v1.1.9 真实故障）
----------------------
页面：``livecamrips.to/video/16402068``，播放器在 ``uqload.vc`` 的 iframe 里。
浏览器里能正常播、猫抓也能拿到地址，但本软件：

- 嗅探：0 个可用地址（真实流被判成占位流丢掉）
- 预览：``Could not open file``
- 下载：``无法解析 / 占位流``

实测（对同一地址做请求头二分，用浏览器捕获的真实头）：

============================  ==========================================
请求头                         结果
============================  ==========================================
Referer = 宿主页 livecamrips    302 → ``/taunt/promo/master.m3u8``
Referer = ``https://uqload.vc/``  200 + 正常 master（单变体 854x480）
完全不带 Referer                 200 + 正常 master
============================  ==========================================

浏览器真实发出的 Referer 是 **播放器自己的域**（``https://uqload.vc/``），
而 ``_build_headers()`` 用 ``firstPartyUrl()``（宿主页）无条件覆盖了它 ——
这就是全部故障的根因。

本测试用本地服务复刻这套校验规则，验证：
1. 对照组：带外来 Referer → 302 到占位清单（复现故障）
2. ``reqhdr.fetch_playlist`` 能自动回退到「目标站自身域」策略并成功
3. 嗅探（``Sniffer.sniff_urls``）拿到视频，且产出的 headers 是**可用**的那套
4. 下载器 ``_fetch_text`` 同样切策略，并把可用头写回 session（供分片使用）
5. 瞬断（SSL EOF）重试：第一次抛错、第二次成功
6. 全部策略都被拒时，报错文案能指导用户（不是误导的"无法解析"）

用法: python tests/test_referer.py
"""
import http.server
import socketserver
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3u8dl import reqhdr
from m3u8dl.downloader import DownloadConfig, M3U8Downloader
from m3u8dl.sniffer import Sniffer

WORK = Path(__file__).resolve().parent.parent / ".test_work" / "referer"

PASS, FAIL = [], []


def check(ok, msg):
    (PASS if ok else FAIL).append(msg)
    print(f"[{'PASS' if ok else 'FAIL'}] {msg}")


# ---------------------------------------------------------------- 模拟站点
MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1233000,RESOLUTION=854x480,CODECS="avc1.4d401f,mp4a.40.2"
index-v1-a1.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:4.0,
seg1.ts
#EXTINF:4.0,
seg2.ts
#EXT-X-ENDLIST
"""

PLACEHOLDER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=1280x720
index.m3u8
"""

SEG = b"\x47" + b"\x11" * 187


class _Site(http.server.BaseHTTPRequestHandler):
    """复刻 uqload 的校验：外来 Referer → 302 占位流。"""

    protocol_version = "HTTP/1.1"
    host_port = 0
    strict_403 = False       # 全部 Referer 都拒（用来测失败分支）

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/octet-stream",
              extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        ref = self.headers.get("Referer") or ""
        path = self.path.split("?")[0]
        own = f"http://127.0.0.1:{type(self).host_port}"

        if path == "/taunt/promo/master.m3u8":
            self._send(200, PLACEHOLDER, "application/vnd.apple.mpegurl")
            return

        # 只有"本站自身域"或"无 Referer"才放行
        foreign = bool(ref) and not ref.startswith(own)
        if type(self).strict_403:
            self._send(403, b"forbidden")
            return
        if foreign:
            self.send_response(302)
            self.send_header("Location", "/taunt/promo/master.m3u8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path.endswith("master.m3u8"):
            self._send(200, MASTER, "application/vnd.apple.mpegurl")
        elif path.endswith("index-v1-a1.m3u8"):
            self._send(200, MEDIA, "application/vnd.apple.mpegurl")
        elif path.endswith(".ts") or "seg" in path:
            self._send(200, SEG, "video/mp2t")
        else:
            self._send(404, b"no")

    do_HEAD = do_GET


def _serve():
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Site)
    httpd.daemon_threads = True
    _Site.host_port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


# ---------------------------------------------------------------- 用例
def test_control_group(port):
    """对照组：带外来 Referer 会被 302 到占位流（复现原故障）。"""
    import requests

    url = f"http://127.0.0.1:{port}/hls/abc/master.m3u8"
    r = requests.get(url, headers={"Referer": "https://livecamrips.to/video/1"},
                     allow_redirects=False)
    check(r.status_code == 302 and "taunt" in r.headers.get("Location", ""),
          f"对照组: 外来 Referer -> {r.status_code} {r.headers.get('Location')}")


def test_fetch_fallback(port):
    """reqhdr 能回退到「目标站自身域」策略。"""
    import requests

    url = f"http://127.0.0.1:{port}/hls/abc/master.m3u8"
    s = requests.Session()
    bad = {"Referer": "https://livecamrips.to/video/1",
           "Origin": "https://livecamrips.to"}
    res = reqhdr.fetch_playlist(s, url, bad, "https://livecamrips.to/video/1",
                                timeout=10)
    check(res.ok, f"回退后拿到清单(variant={res.variant})")
    check(res.variant == "site_root", f"生效策略是 site_root: {res.variant}")
    check((res.headers.get("Referer") or "").startswith(
        f"http://127.0.0.1:{port}"), f"可用 Referer: {res.headers.get('Referer')}")
    check("#EXTM3U" in res.text and "854x480" in res.text, "清单内容正确")


def test_sniffer(port):
    """嗅探产出可用条目，且 headers 是实测可用的那套。"""
    url = f"http://127.0.0.1:{port}/hls/abc/master.m3u8"
    hmap = {url: {"Referer": "https://livecamrips.to/video/1",
                  "Origin": "https://livecamrips.to"}}
    s = Sniffer(timeout=10, log=lambda m: None)
    infos = s.sniff_urls([url], "https://livecamrips.to/video/1",
                         "test", hmap)
    check(len(infos) >= 1, f"嗅探拿到 {len(infos)} 条(应为 1)")
    if not infos:
        return
    i = infos[0]
    check(i.resolution == "854x480" and i.bitrate_kbps == 1233,
          f"清晰度/码率: {i.resolution} / {i.bitrate_kbps}kbps")
    check((i.headers.get("Referer") or "").startswith(f"http://127.0.0.1:{port}"),
          "条目 headers 换成可用 Referer(供下载/预览复用)")
    check(i.duration_text.endswith("s") and i.duration_text != "0s",
          f"时长: {i.duration_text}")


def test_downloader(port):
    """下载器能切策略取清单，并把可用头写回 session（供分片用）。"""
    WORK.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{port}/hls/abc/master.m3u8"
    cfg = DownloadConfig(
        url=url, save_dir=str(WORK), save_name="ref_test",
        headers={"Referer": "https://livecamrips.to/video/1"},
        threads=2, retry=3, timeout=10, binary_merge=True,
        page_url="https://livecamrips.to/video/1",
    )
    logs = []
    dl = M3U8Downloader(cfg, on_log=logs.append,
                        on_progress=lambda d: None,
                        on_state=lambda s: None)
    try:
        ok = dl.run()
    except Exception as e:
        check(False, f"下载异常: {e}")
        return
    out = WORK / "ref_test.ts"
    check(ok is not False and out.exists() and out.stat().st_size > 100,
          f"下载完成: {out.name if out.exists() else '无文件'} "
          f"{out.stat().st_size if out.exists() else 0} 字节")
    ref = dl.session.headers.get("Referer") or ""
    check(ref.startswith(f"http://127.0.0.1:{port}"),
          f"session 已切到可用 Referer: {ref}")
    check(any("策略" in m for m in logs),
          f"日志记录了策略切换: {[m for m in logs if '策略' in m][:1]}")


def test_transient_retry(port):
    """瞬断(SSL EOF)自动重试：第 1 次抛错, 第 2 次成功。"""
    import requests

    url = f"http://127.0.0.1:{port}/hls/abc/master.m3u8"
    real_get = requests.Session.get
    state = {"n": 0}

    def flaky(self, *a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")
        return real_get(self, *a, **kw)

    requests.Session.get = flaky
    try:
        s = requests.Session()
        res = reqhdr.fetch_playlist(
            s, url, {"Referer": f"http://127.0.0.1:{port}/"},
            timeout=10)
    finally:
        requests.Session.get = real_get
    check(res.ok and res.retries >= 1,
          f"瞬断重试成功(重试 {res.retries} 次, 调用 {state['n']} 次)")


def test_all_rejected(port):
    """所有策略都被 403 时，报错文案要能指导用户。"""
    _Site.strict_403 = True
    try:
        import requests

        url = f"http://127.0.0.1:{port}/hls/abc/master.m3u8"
        res = reqhdr.fetch_playlist(requests.Session(), url, {}, timeout=10)
        check(not res.ok, "全部被拒时返回失败")
        msg = reqhdr.describe_failure(res)
        check("403" in msg, f"失败原因可指导用户: {msg}")
    finally:
        _Site.strict_403 = False


def test_placeholder_still_skipped(port):
    """回归：直接请求占位清单仍被识别为占位流。"""
    import requests

    res = reqhdr.fetch_playlist(
        requests.Session(), f"http://127.0.0.1:{port}/taunt/promo/master.m3u8",
        {}, timeout=10)
    check(not res.ok and res.redirect_only,
          "占位清单被拒绝(不当作真视频)")


def test_build_headers_keeps_captured_referer():
    """_build_headers: 捕获到的 Referer 优先于 firstPartyUrl。"""
    from m3u8dl.ui.main_window import MainWindow

    fake = SimpleNamespace(_cookies=None,
                          _REPLAY_HEADERS=MainWindow._REPLAY_HEADERS)
    defaults = {"User-Agent": "UA", "Referer": "https://host/page",
                "Origin": "https://host", "Accept": "*/*"}
    item = {
        "url": "https://strm13.uqload.vc/hls2/x/master.m3u8?t=abc",
        "headers": {"Referer": "https://uqload.vc/",
                    "Origin": "https://uqload.vc",
                    "Accept": "*/*"},
        "referer": "https://livecamrips.to/video/16402068",   # firstPartyUrl
    }
    h = MainWindow._build_headers(fake, item, defaults)
    check(h.get("Referer") == "https://uqload.vc/",
          f"捕获 Referer 未被宿主页覆盖: {h.get('Referer')}")
    check(h.get("Origin") == "https://uqload.vc",
          f"捕获 Origin 保留: {h.get('Origin')}")

    # 捕获头里没有 Referer 时, 才用 firstPartyUrl 兜底
    item2 = {"url": "https://cdn.x/a.m3u8", "headers": {"Accept": "*/*"},
             "referer": "https://livecamrips.to/video/1"}
    h2 = MainWindow._build_headers(fake, item2, defaults)
    check(h2.get("Referer") == "https://livecamrips.to/video/1",
          f"无捕获 Referer 时用 firstPartyUrl 兜底: {h2.get('Referer')}")


def main():
    srv, port = _serve()
    test_build_headers_keeps_captured_referer()
    test_control_group(port)
    test_fetch_fallback(port)
    test_sniffer(port)
    test_downloader(port)
    test_transient_retry(port)
    test_placeholder_still_skipped(port)
    test_all_rejected(port)
    srv.shutdown()

    print()
    if FAIL:
        print(f"REFERER TEST FAILED: {len(FAIL)} 项失败")
        for f in FAIL:
            print("  -", f)
        return 1
    print(f"REFERER TEST OK: {len(PASS)} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
