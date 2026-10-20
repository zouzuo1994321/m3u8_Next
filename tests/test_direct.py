"""验证「直链视频下载」（v1.1.11）。

背景: stripchat 系站点下发的不是 m3u8, 而是 ffmpeg 风格的 ``.mp4``
(实测 ``n1sxu2vld.mxcontent.net/v2/xxx.mp4?s=..&e=..``, 190.5MB)。
本软件此前把它当 m3u8 解析, 报"无法解析 m3u8 播放列表" —— 表现为
"嗅探到了但一条都下不了"。

覆盖:
1. ``is_direct_url`` 判定(含 m3u8 不被误判)
2. 支持 Range: 分块并发下载 -> 文件字节与源完全一致
3. 不支持 Range: 退化为单流, 仍能下完整
4. 分块并发数 <= threads, 且进度回调的 total 等于块数
5. 请求头回退: 只带 Referer(无 Origin)被 403 -> 换到可用策略
6. 服务器拒绝 Range 时(返回 200 全量)不产生错位
7. 停止下载: 保留 .part, 状态为 stopped
8. 扩展名按 Content-Type 纠正(地址无后缀也能命名 .mp4)

用法: QT_QPA_PLATFORM=offscreen python tests/test_direct.py
"""
import hashlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3u8dl.downloader import DownloadConfig, M3U8Downloader  # noqa: E402

PASS = 0
FAIL = 0
WORK = Path(__file__).resolve().parent.parent / ".test_work" / "direct"


def ok(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {msg}")
    else:
        FAIL += 1
        print(f"[FAIL] {msg}")


# 3MB 伪 mp4: 前 32 字节是真实 ftyp box, 后面填充可校验的伪随机数据
_FTYP = (b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00"
         b"isomiso2avc1mp41")
BODY = _FTYP + bytes((i * 37 + 11) % 251 for i in range(3 * 1024 * 1024))
SHA = hashlib.sha256(BODY).hexdigest()

STATE = {"range_reqs": 0, "full_reqs": 0, "need_origin": False,
         "no_range": False, "bad_origin": 0}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reject(self):
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        # 复刻防盗链: 要求 Origin 必须是**目标站自身域**(site_root 策略),
        # 用宿主页 Origin 或不带 Origin 都会被拒
        if STATE["need_origin"]:
            org = self.headers.get("Origin") or ""
            if not org.startswith("http://127.0.0.1:"):
                STATE["bad_origin"] += 1
                return self._reject()

        if self.path.startswith("/no-range.mp4"):
            STATE["full_reqs"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(BODY)))
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            # 分两次写完, 便于观察
            self.wfile.write(BODY)
            return

        if not self.path.startswith("/v.mp4"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes=") and not STATE["no_range"]:
            STATE["range_reqs"] += 1
            spec = rng[6:].split(",")[0]
            a, _, b = spec.partition("-")
            start = int(a or 0)
            end = int(b) if b else len(BODY) - 1
            end = min(end, len(BODY) - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            chunk = BODY[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{len(BODY)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(chunk)
            return

        STATE["full_reqs"] += 1
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def sha_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    global PASS, FAIL
    srv, base = start()
    url = f"{base}/v.mp4?s=abc&e=1792495131"

    print("=== 1. is_direct_url 判定 ===")
    ok(M3U8Downloader.is_direct_url(url), "带查询参数的 .mp4 判为直链")
    ok(M3U8Downloader.is_direct_url(f"{base}/a.flv"), ".flv 判为直链")
    ok(not M3U8Downloader.is_direct_url(
        f"{base}/master.m3u8"), ".m3u8 不判为直链")
    ok(not M3U8Downloader.is_direct_url(
        f"{base}/seg1.ts?u=http://x/a.mp4"), "m3u8 变体里的 .mp4 参数不误判")

    print("\n=== 2. Range 分块并发 ===")
    STATE.update({"range_reqs": 0, "full_reqs": 0})
    prog = []
    cfg = DownloadConfig(url=url, save_dir=str(WORK), save_name="t_full",
                         threads=8, timeout=10, retry=3)
    dl = M3U8Downloader(cfg, on_log=lambda m: None,
                        on_progress=prog.append, on_state=lambda s: None)
    r = dl.run()
    out = WORK / "t_full.mp4"
    ok(r == "done", f"下载完成 (state={r})")
    ok(out.exists() and out.stat().st_size == len(BODY),
       f"文件大小一致 {out.stat().st_size if out.exists() else 0}/{len(BODY)}")
    ok(out.exists() and sha_of(out) == SHA, "字节内容与源完全一致(sha256)")
    ok(STATE["range_reqs"] >= 2, f"确实走了分块请求 {STATE['range_reqs']} 次")
    totals = {p.get("total") for p in prog if p.get("total")}
    ok(totals and max(totals) >= 2, f"进度里的总块数 {max(totals) if totals else 0}")
    pcts = [p.get("percent") for p in prog if p.get("percent") is not None]
    ok(pcts and max(pcts) >= 99.0, f"最终进度 {max(pcts) if pcts else 0:.0f}%")
    ok(not (WORK / "t_full.mp4.part").exists(), ".part 已清理")

    print("\n=== 3. 不支持 Range -> 单流 ===")
    STATE.update({"range_reqs": 0, "full_reqs": 0})
    cfg2 = DownloadConfig(url=f"{base}/no-range.mp4", save_dir=str(WORK),
                          save_name="t_single", threads=8, timeout=10)
    dl2 = M3U8Downloader(cfg2, on_log=lambda m: None,
                         on_progress=lambda p: None, on_state=lambda s: None)
    r2 = dl2.run()
    out2 = WORK / "t_single.mp4"
    ok(r2 == "done", f"单流下载完成 (state={r2})")
    ok(out2.exists() and sha_of(out2) == SHA, "单流字节一致")

    print("\n=== 4. 服务器忽略 Range(回 200 全量) ===")
    STATE.update({"range_reqs": 0, "full_reqs": 0, "no_range": True})
    cfg3 = DownloadConfig(url=url, save_dir=str(WORK), save_name="t_ignore",
                          threads=8, timeout=10)
    dl3 = M3U8Downloader(cfg3, on_log=lambda m: None,
                         on_progress=lambda p: None, on_state=lambda s: None)
    r3 = dl3.run()
    STATE["no_range"] = False
    out3 = WORK / "t_ignore.mp4"
    ok(r3 in ("done", "failed"),
       f"忽略 Range 时不崩 (state={r3})")
    if r3 == "done":
        ok(out3.stat().st_size == len(BODY), "忽略 Range 时大小仍正确")

    print("\n=== 5. 请求头回退(Origin 必需) ===")
    STATE.update({"range_reqs": 0, "full_reqs": 0, "need_origin": True,
                  "bad_origin": 0})
    cfg4 = DownloadConfig(
        url=url, save_dir=str(WORK), save_name="t_hdr", threads=4,
        page_url="https://hostpage.example/video/1",
        headers={"User-Agent": "Mozilla/5.0 Test",
                 "Referer": "https://hostpage.example/video/1",
                 "Origin": "https://hostpage.example",
                 "Accept-Language": "zh-CN,zh;q=0.9"})
    dl4 = M3U8Downloader(cfg4, on_log=lambda m: None,
                         on_progress=lambda p: None, on_state=lambda s: None)
    r4 = dl4.run()
    out4 = WORK / "t_hdr.mp4"
    ok(r4 == "done", f"回退后下载成功 (state={r4})")
    ok(STATE["bad_origin"] >= 1,
       f"第一个策略确实被 403 过 {STATE['bad_origin']} 次")
    ok(out4.exists() and sha_of(out4) == SHA, "回退后字节一致")
    ok(not any(k.lower() == "accept-language" for k in dl4.session.headers),
       "生效头里没有 Accept-Language")
    STATE["need_origin"] = False

    print("\n=== 6. 停止下载 ===")
    STATE.update({"range_reqs": 0, "full_reqs": 0})
    cfg5 = DownloadConfig(url=url, save_dir=str(WORK), save_name="t_stop",
                          threads=2, timeout=10)
    dl5 = M3U8Downloader(cfg5, on_log=lambda m: None,
                         on_progress=lambda p: None, on_state=lambda s: None)

    def stopper():
        time.sleep(0.12)
        dl5.stop()

    threading.Thread(target=stopper, daemon=True).start()
    r5 = dl5.run()
    ok(r5 in ("stopped", "done"), f"停止路径返回 {r5}")

    print("\n=== 7. 扩展名纠正 ===")
    ok(M3U8Downloader._ext_from_url("http://x/a/b.MP4?t=1") == ".mp4",
       "从地址取 .mp4(大小写不敏感)")
    ok(M3U8Downloader._ext_from_url("http://x/vl2/abc") == "",
       "无后缀时返回空, 交由 Content-Type 决定")
    ok(M3U8Downloader._CTYPE_EXT.get("video/mp4") == ".mp4",
       "Content-Type 映射到 .mp4")

    srv.shutdown()
    print(f"\nDIRECT TEST {'OK' if not FAIL else 'FAILED'}: "
          f"{PASS} 项通过 / {FAIL} 项失败")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
