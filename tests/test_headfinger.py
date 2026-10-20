"""验证「请求头集合指纹」导致的 403 —— v1.1.10 根因。

真实案例(2026-10-20 实测):
``happyams1.streambucket.xyz`` 对**多出来的头**直接 403 —— 只要请求里出现
``Accept-Language``(无论 zh-CN 还是 en-US 还是 en), 一律拒; 去掉就 200。
而这条头并非浏览器发的, 是主程序 defaults 里写死的, 经 ``h = dict(defaults)``
被带进每一次请求。v1.1.9 修好了 Referer 却仍失败, 就是卡在这里。

本测试用本地服务器复刻该行为, 覆盖:
1. 站点复刻: 带 Accept-Language -> 403, 不带 -> 200
2. ``_build_headers``: 捕获头存在时不再继承 defaults 的浏览器特征头
3. ``_build_headers``: 捕获头为空时仍用 defaults 兜底(直接粘贴 m3u8 的场景)
4. ``reqhdr.minimal_headers``: 只留 UA/Referer/Origin/鉴权头
5. 策略序列含 minimal, 且排在 site_root 之前
6. 端到端: 被下毒的请求头 -> captured 403 -> minimal 200
7. 下载器 ``_adopt_headers``: **整体替换** session 头, 脏 Accept-Language 被清掉
8. 分片: 用替换后的干净头 -> 200

用法: QT_QPA_PLATFORM=offscreen python tests/test_headfinger.py
"""
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from m3u8dl import reqhdr  # noqa: E402

PASS = 0
FAIL = 0


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {msg}")
    else:
        FAIL += 1
        print(f"[FAIL] {msg}")


M3U8 = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        "#EXT-X-MEDIA-SEQUENCE:1\n#EXTINF:4.0,\nseg1.ts\n"
        "#EXTINF:4.0,\nseg2.ts\n#EXT-X-ENDLIST\n")

SEG = b"\x47" + b"\x00" * 187
STATS = {"403": 0, "200": 0, "seg_ok": 0, "seg_bad": 0}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        # 复刻"头集合指纹": 出现 Accept-Language 就 403
        if "Accept-Language" in self.headers or "accept-language" in {
                k.lower() for k in self.headers.keys()}:
            STATS["403"] += 1
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/stream.m3u8"):
            STATS["200"] += 1
            body = M3U8.encode()
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/seg1.ts"):
            # 分片同样查头指纹
            STATS["seg_ok"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(SEG)))
            self.end_headers()
            self.wfile.write(SEG)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


DEFAULTS = {
    "User-Agent": "Mozilla/5.0 (Test) Chrome/140.0.0.0",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class FakeMW:
    """只为调用 MainWindow._build_headers: 它只用到这两个属性。"""

    from m3u8dl.ui.main_window import MainWindow as _MW
    _REPLAY_HEADERS = _MW._REPLAY_HEADERS
    _cookies = None


def main():
    global PASS, FAIL
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841

    from m3u8dl.ui.main_window import MainWindow
    from m3u8dl.downloader import DownloadConfig, M3U8Downloader

    srv, base = start_server()
    url = f"{base}/stream.m3u8"
    page = "https://host.example/video/1"

    print("=== 1. 站点复刻 ===")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0",
                                       "Accept-Language": "zh-CN"},
                         timeout=10)
        ok(r.status_code == 403, "带 Accept-Language -> 403(复刻成功)")
    except Exception as e:
        ok(False, f"带 AL 请求异常: {e}")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    ok(r.status_code == 200 and "#EXTM3U" in r.text,
       "不带 Accept-Language -> 200 且清单正常")

    print("\n=== 2. _build_headers 不继承 defaults ===")
    mw = FakeMW()
    item = {
        "url": url,
        "referer": page,
        "headers": {"User-Agent": "Mozilla/5.0 (Test) Chrome/140.0.0.0",
                    "Accept": "*/*",
                    "Origin": "https://abstream.to",
                    "Referer": "https://abstream.to/",
                    "sec-ch-ua": '"Chromium";v="140"'},
    }
    h = MainWindow._build_headers(mw, item, DEFAULTS)
    ok(not any(k.lower() == "accept-language" for k in h),
       f"捕获头存在时不带 Accept-Language: {sorted(h)}")
    ok(h.get("Referer") == "https://abstream.to/",
       "捕获的 Referer 优先(未被宿主页覆盖)")
    ok(h.get("Origin") == "https://abstream.to", "捕获的 Origin 保留")
    ok(any(k.lower() == "user-agent" for k in h), "UA 保留")

    print("\n=== 3. 捕获头为空时用 defaults 兜底 ===")
    h2 = MainWindow._build_headers(mw, {"url": url, "headers": {},
                                       "referer": page}, DEFAULTS)
    ok(h2.get("Accept-Language") == DEFAULTS["Accept-Language"],
       "无捕获头时才使用 defaults(含 Accept-Language)")
    ok(h2.get("Referer") == page, "无捕获 Referer 时用 firstPartyUrl 兜底")

    print("\n=== 4. minimal_headers ===")
    mini = reqhdr.minimal_headers({
        "User-Agent": "UA", "Referer": "R", "Origin": "O",
        "Cookie": "c=1", "Authorization": "Bearer x",
        "Accept": "*/*", "Accept-Language": "zh-CN",
        "sec-ch-ua": "x", "X-Token": "t",
    })
    ok("Accept-Language" not in mini and "Accept" not in mini,
       f"噪音头被剥离(剩 {sorted(mini)})")
    ok("User-Agent" in mini and "Referer" in mini and "Origin" in mini,
       "防盗链必需头保留")
    ok("Cookie" in mini and "Authorization" in mini, "鉴权头保留")
    ok("X-Token" in mini, "自定义 x-* 头保留")

    print("\n=== 5. 策略序列 ===")
    names = [n for n, _ in reqhdr.build_variants(
        url, {**DEFAULTS, "Referer": "https://abstream.to/"}, page)]
    ok("minimal" in names, f"序列含 minimal: {names}")
    ok(names.index("minimal") < names.index("site_root"),
       "minimal 排在 site_root 之前")

    print("\n=== 6. 端到端: 下毒头 -> minimal 命中 ===")
    dirty = {"User-Agent": "Mozilla/5.0 (Test) Chrome/140.0.0.0",
             "Accept": "*/*",
             "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
             "Referer": "https://abstream.to/"}
    before = dict(STATS)
    logs = []
    sess = requests.Session()
    sess.headers.update({"User-Agent": dirty["User-Agent"]})
    res = reqhdr.fetch_playlist(sess, url, dirty, page, 10, log=logs.append)
    ok(res.ok, f"最终拿到清单(策略={res.variant})")
    ok(res.variant == "minimal", f"生效策略是 minimal: {res.variant}")
    ok(STATS["403"] - before["403"] >= 1,
       f"captured 档确实被 403 过 {STATS['403'] - before['403']} 次")
    ok(not any(k.lower() == "accept-language" for k in res.headers),
       "返回的可用头里没有 Accept-Language")
    ok(any("minimal" in str(x) for x in logs),
       f"日志记录了策略切换: {logs[:1]}")

    print("\n=== 7. 下载器 session 头管理 ===")
    cfg = DownloadConfig(url=url, page_url=page, headers=dirty,
                         save_dir=str(Path(__file__).parent.parent /
                                      ".test_work"), save_name="hf")
    dl = M3U8Downloader(cfg, on_log=lambda m: None,
                        on_progress=lambda a, b, c: None,
                        on_state=lambda s: None)
    ok(not any(k.lower() == "accept-language" for k in dl.session.headers),
       f"构造后未把候选头灌进 session: {sorted(dl.session.headers)}")
    # 人为往 session 注入脏头: requests 会把它合并进每次请求, 必须被摘掉
    dl.session.headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    ok(any(k.lower() == "accept-language" for k in dl.session.headers),
       "脏 Accept-Language 已注入(前置条件)")
    text = dl._fetch_text(url)
    ok("#EXM3U" in text or "#EXTM3U" in text, "清单文本正确")
    ok(not any(k.lower() == "accept-language" for k in dl.session.headers),
       f"session 脏头被清除: {sorted(dl.session.headers)}")
    ok(any(k.lower() == "referer" for k in dl.session.headers),
       "session 保留了可用的 Referer")

    print("\n=== 8. 分片用干净头可取 ===")
    s0 = requests.get(f"{base}/seg1.ts", headers=dict(dl.session.headers),
                      timeout=10)
    ok(s0.status_code == 200 and s0.content[:1] == b"\x47",
       f"分片 200 且 TS 同步字节正确({len(s0.content)} 字节)")

    srv.shutdown()
    print(f"\nHEAD FINGERPRINT TEST {'OK' if not FAIL else 'FAILED'}: "
          f"{PASS} 项通过 / {FAIL} 项失败")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
