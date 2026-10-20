"""CDP 调试: 打印浏览器发来的**所有**事件与请求, 定位抓不到的原因。

用法:
    python tests/probe_cdp_dbg.py [页面地址]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtWebSockets import QWebSocket  # noqa: E402

from m3u8dl import cdp  # noqa: E402

PAGE = (sys.argv[1] if len(sys.argv) > 1
        else "https://livecamrips.to/video/16401088")
SECONDS = 18

app = QApplication(sys.argv)
name, exe = cdp.find_browser()
print(f"[browser] {name} {exe}")
if not exe:
    raise SystemExit(1)

import os  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

port = cdp._free_port()
profile = os.path.join(tempfile.gettempdir(), "m3u8dl_cdp_dbg")
os.makedirs(profile, exist_ok=True)
proc = subprocess.Popen([
    exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
    "--no-first-run", "--no-default-browser-check",
    "--autoplay-policy=no-user-gesture-required",
    PAGE,
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

n_evt = {"n": 0}
n_req = {"n": 0}
ctxs = {}

ws = QWebSocket()
msg_id = {"v": 0}


def send(method, params=None):
    msg_id["v"] += 1
    ws.sendTextMessage(json.dumps(
        {"id": msg_id["v"], "method": method, "params": params or {}}))


def on_open():
    print("[ws] 已连接", flush=True)
    send("Network.enable")
    send("Page.enable")
    send("Runtime.enable")


def on_msg(text):
    try:
        d = json.loads(text)
    except Exception:
        return
    m = d.get("method")
    if not m:
        if "error" in d:
            print(f"[err] id={d.get('id')} {d['error']}", flush=True)
        else:
            r = d.get("result") or {}
            rr = r.get("result") or {}
            print(f"[ok ] id={d.get('id')} val={rr.get('value')} "
                  f"desc={str(rr.get('description'))[:80]}", flush=True)
        return
    n_evt["n"] += 1
    if m == "Runtime.executionContextCreated":
        c = (d.get("params") or {}).get("context") or {}
        ctxs[c.get("id")] = c.get("origin")
        print(f"[ctx+] {c.get('id')} {c.get('origin')} {c.get('name')}",
              flush=True)
        return
    if m == "Network.requestWillBeSent":
        n_req["n"] += 1
        r = (d.get("params") or {}).get("request") or {}
        u = r.get("url", "")
        if n_req["n"] <= 60:
            print(f"[req {n_req['n']:3d}] {u[:130]}", flush=True)
        return
    if m in ("Page.frameNavigated", "Page.loadEventFired"):
        p = d.get("params") or {}
        fr = p.get("frame") or {}
        print(f"[{m}] {str(fr.get('url') or '')[:110]}", flush=True)


ws.connected.connect(on_open)
ws.textMessageReceived.connect(on_msg)

poll_tries = {"n": 0}
poll = QTimer()
poll.setInterval(400)


def probe():
    import requests
    poll_tries["n"] += 1
    if poll_tries["n"] > 40:
        print("[!] 探测超时", flush=True)
        poll.stop()
        return
    try:
        r = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=1.5)
        tgs = r.json()
    except Exception:
        return
    pages = [t for t in tgs if t.get("type") == "page"
             and str(t.get("url", "")).startswith("http")]
    if not pages:
        if poll_tries["n"] % 5 == 0:
            print("[json/list] " + str([(t.get('type'), str(t.get('url'))[:60])
                                        for t in tgs]), flush=True)
        return
    poll.stop()
    print(f"[target] {pages[0]['url'][:110]}", flush=True)
    ws.open(QUrl(pages[0]["webSocketDebuggerUrl"]))


poll.timeout.connect(probe)
poll.start()

poke = QTimer()
poke.setInterval(2500)


def do_poke():
    if not ctxs:
        print("[poke] 无 context", flush=True)
        return
    for cid in list(ctxs):
        print(f"[poke] ctx={cid}", flush=True)
        send("Runtime.evaluate", {"expression": cdp._POKE_JS,
                                  "contextId": cid, "returnByValue": True})


def done():
    print(f"\n=== 事件 {n_evt['n']} 个, 请求 {n_req['n']} 个, "
          f"context {len(ctxs)} 个 ===", flush=True)
    print("contexts:", ctxs, flush=True)
    ws.close()
    proc.terminate()
    app.quit()


QTimer.singleShot(SECONDS * 1000, done)
poke.timeout.connect(do_poke)
QTimer.singleShot(6000, poke.start)
app.exec()
