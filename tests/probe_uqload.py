"""诊断脚本: 复现 uqload 的 HLS 请求被 302 到 taunt/promo 的原因。

背景: livecamrips 这类聚合页把播放器放在 uqload iframe 里, 浏览器里能正常播,
猫抓也能拿到地址; 但本软件用 requests 二次请求同一地址时被 302 到
``/taunt/promo/master.m3u8``(占位流), 于是判定为"token 失效"跳过。

本脚本做两件事:
1. 用内置浏览器引擎真实加载页面, 把捕获到的 m3u8 请求**连同完整请求头、
   Referer、Cookie** 原样落盘(不截断)。
2. 立刻用 requests 对该地址做**请求头二分回放**, 对比状态码, 定位到底是
   哪个头(或哪一类绑定)导致被拒。

用法:
    python tests/probe_uqload.py [页面地址]

输出: .probe/uqload_<时间戳>.json + 控制台对比表
"""
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      "--autoplay-policy=no-user-gesture-required")

import requests  # noqa: E402
from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtWebEngineCore import (  # noqa: E402
    QWebEngineProfile,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402

from m3u8dl import webinject  # noqa: E402
from m3u8dl.cookies import ProfileCookies  # noqa: E402

PAGE = (sys.argv[1] if len(sys.argv) > 1
        else "https://livecamrips.to/video/16402068")
OUT = Path(__file__).resolve().parent.parent / ".probe"
SETTLE_MS = 22000

HITS = []
LOCK = threading.Lock()


class Rec(QWebEngineUrlRequestInterceptor):
    """记录所有 m3u8 / mp4 / media 请求(完整 URL + 完整请求头)。"""

    def interceptRequest(self, info):
        try:
            url = info.requestUrl().toString()
        except Exception:
            return
        low = url.lower()
        if not (".m3u8" in low or "m3u8" in low or ".mp4" in low
                or "/hls" in low):
            return
        if any(x in low for x in (".ts?", ".ts&", ".m4s", ".key")):
            return
        headers = {}
        try:
            for k, v in (info.httpHeaders() or {}).items():
                try:
                    ks = bytes(k).decode("latin-1")
                    vs = bytes(v).decode("latin-1")
                except Exception:
                    ks, vs = str(k), str(v)
                if ks:
                    headers[ks] = vs
        except Exception:
            pass
        try:
            ref = info.firstPartyUrl().toString()
        except Exception:
            ref = ""
        try:
            rt = str(info.resourceType()).rsplit(".", 1)[-1]
        except Exception:
            rt = "?"
        with LOCK:
            if any(h["url"] == url for h in HITS):
                return
            HITS.append({"url": url, "headers": headers, "referer": ref,
                         "type": rt, "ts": time.time()})


def probe(url: str, headers: dict, tag: str, allow_redirects=False):
    """发一次请求, 返回 (状态码, 最终地址, Location)。"""
    try:
        r = requests.get(url, headers=headers, timeout=20,
                         allow_redirects=allow_redirects)
        return r.status_code, r.url, r.headers.get("Location", "")
    except Exception as e:
        return -1, "", repr(e)[:120]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)

    profile = QWebEngineProfile.defaultProfile()
    ua = profile.httpUserAgent()
    chrome_ua = ua.replace("QtWebEngine/6.11.2 ", "")
    profile.setHttpUserAgent(chrome_ua)
    jar = ProfileCookies(profile)

    rec = Rec()
    profile.setUrlRequestInterceptor(rec)
    # 与主程序一致: 伪装编解码器支持, 否则播放器在 isSupported() 阶段就放弃,
    # 连 m3u8 都不会请求(诊断会误判成"页面没有视频")
    webinject.install(profile)

    view = QWebEngineView()
    view.resize(1000, 700)
    view.show()

    state = {"loaded": False}
    done = {"v": False}

    CLICK_JS = r"""
    (function () {
      var out = [];
      try {
        var vs = document.querySelectorAll('video');
        for (var i = 0; i < vs.length; i++) {
          try { vs[i].muted = true; vs[i].play(); out.push('video'+i); } catch(e){}
        }
        var sel = ['[class*=play i]', '[id*=play i]', '.play-button',
                   '[aria-label*=play i]'];
        for (var s = 0; s < sel.length; s++) {
          var els = document.querySelectorAll(sel[s]);
          for (var k = 0; k < Math.min(els.length, 6); k++) {
            try { els[k].click(); out.push(sel[s]+':'+k); } catch(e){}
          }
        }
        var fs = document.querySelectorAll('iframe');
        for (var j = 0; j < fs.length; j++) { out.push('iframe=' + fs[j].src); }
      } catch (e) { out.push('err:' + e); }
      return out.join(' | ');
    })()
    """

    def click_play():
        view.page().runJavaScript(
            CLICK_JS, 0,
            lambda r: print(f"[click] {str(r)[:400]}"))

    def finish():
        if done["v"]:
            return
        done["v"] = True
        report(jar)
        app.quit()

    def on_loaded(ok):
        state["loaded"] = ok
        print(f"[load] ok={ok} title={view.title()!r}")
        if ok:
            # 有些站点要等用户点播放才建 iframe / 起播, 这里自动点一次
            QTimer.singleShot(7000, click_play)
            QTimer.singleShot(SETTLE_MS, finish)

    view.page().loadFinished.connect(on_loaded)
    view.setUrl(QUrl(PAGE))
    print(f"[open] {PAGE}")
    QTimer.singleShot(SETTLE_MS + 30000, finish)

    app.exec()


def report(jar):
    with LOCK:
        hits = list(HITS)
    print(f"\n=== 捕获 {len(hits)} 条候选 ===")
    for h in hits:
        print(f"\n[{h['type']}] {h['url']}")
        print(f"  referer={h['referer']}")
        for k, v in sorted(h["headers"].items()):
            print(f"  {k}: {str(v)[:160]}")

    m3u8 = [h for h in hits if ".m3u8" in h["url"].lower()]
    if not m3u8:
        print("!! 没捕获到 m3u8, 无法继续对比")
        save(hits, [])
        return

    target = m3u8[0]
    url = target["url"]
    host = urlparse(url).hostname or ""
    jar_header = jar.header_for(url)
    print(f"\n=== Cookie 罐 ===")
    print(f"  summary: {jar.summary()}")
    print(f"  for {host}: {jar_header[:200] or '(空)'}")

    cap = dict(target["headers"])
    cap.pop("Cookie", None)
    cap.pop("cookie", None)
    variants = [
        ("A 完全无自定义头", {"User-Agent": "Mozilla/5.0"}),
        ("B 仅页面 Referer", {"User-Agent": "Mozilla/5.0",
                          "Referer": target["referer"]}),
        ("C 浏览器捕获头(原样)", cap),
        ("D 捕获头 + Cookie罐", {**cap, **({"Cookie": jar_header}
                                        if jar_header else {})}),
        ("E 捕获头, 去掉 Referer",
         {k: v for k, v in cap.items() if k.lower() != "referer"}),
        ("F 捕获头, Referer=自身域",
         {**cap, "Referer": f"https://{host}/"}),
        ("G 仅 Cookie罐", {"User-Agent": cap.get("User-Agent", "Mozilla/5.0"),
                        **({"Cookie": jar_header} if jar_header else {})}),
    ]

    rows = []
    print("\n=== 请求头二分回放(不跟随重定向) ===")
    for tag, h in variants:
        code, final, loc = probe(url, h, tag, allow_redirects=False)
        mark = "占位流" if "taunt" in loc or "promo" in loc else (
            "OK" if 200 <= code < 300 else "")
        print(f"  {tag:24s} -> {code} {mark}  {loc[:90]}")
        rows.append({"tag": tag, "code": code, "final": final,
                     "location": loc})

    # 跟随重定向看最终落到哪
    code, final, _ = probe(url, cap, "C", allow_redirects=True)
    print(f"\n  [跟随重定向] 捕获头 -> {code} final={final[:120]}")
    rows.append({"tag": "C+redirect", "code": code, "final": final,
                 "location": ""})

    save(hits, rows)


def save(hits, rows):
    p = OUT / f"uqload_{int(time.time())}.json"
    p.write_text(json.dumps({"hits": hits, "probes": rows},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[saved] {p}")


if __name__ == "__main__":
    main()
