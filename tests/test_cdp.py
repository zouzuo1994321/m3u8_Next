"""系统浏览器嗅探(CDP) 单元测试。

不启动真实浏览器 —— 只用合成的 CDP 消息验证:
  * 事件分流(sessionId / OOPIF 自动附加)
  * 视频地址过滤(m3u8 / mp4 命中, ts 分片放过)
  * 请求头与 Referer 记录
  * executionContext 记录与逐 context 起播
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3u8dl.cdp import BrowserSniffer, find_browser  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


class FakeWS:
    """假的 QWebSocket: 只记录发出去的命令。"""

    def __init__(self):
        self.sent = []

    def sendTextMessage(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        pass

    def deleteLater(self):
        pass


def feed(sn, method, params, session=""):
    msg = {"method": method, "params": params}
    if session:
        msg["sessionId"] = session
    sn._on_message(json.dumps(msg))


print("=== 1. 浏览器探测 ===")
name, exe = find_browser()
check("find_browser 返回二元组", isinstance(name, str) and isinstance(exe, str))
check("找到时路径存在", (not exe) or Path(exe).is_file(), exe)
print(f"  结果: {name or '(无)'} {exe or ''}")

print("=== 2. 初始状态 ===")
sn = BrowserSniffer()
check("默认未激活", sn.active is False)
check("hits 为空", sn.hits == [])
check("contexts 为空", sn._contexts == {})

print("=== 3. 自动附加(OOPIF) ===")
sn._ws = FakeWS()
sn._on_open()
methods = [m["method"] for m in sn._ws.sent]
check("发了 Network.enable", "Network.enable" in methods)
check("发了 Runtime.enable", "Runtime.enable" in methods, methods)
check("发了 Target.setAutoAttach", "Target.setAutoAttach" in methods)
attach = [m for m in sn._ws.sent if m["method"] == "Target.setAutoAttach"][0]
check("autoAttach 为 True", attach["params"]["autoAttach"] is True)
check("flatten 为 True(子 target 复用同一连接)",
      attach["params"]["flatten"] is True)

sn._ws.sent.clear()
feed(sn, "Target.attachedToTarget",
     {"sessionId": "S1", "targetInfo": {"type": "iframe"}})
s1 = [m for m in sn._ws.sent if m.get("sessionId") == "S1"]
check("对 iframe 子 target 开了 Network.enable",
      any(m["method"] == "Network.enable" for m in s1))

print("=== 4. executionContext 与起播 ===")
sn._ws.sent.clear()
feed(sn, "Runtime.executionContextCreated",
     {"context": {"id": 7, "auxData": {"isDefault": True}}}, session="S1")
check("记录了 context(S1|7)", "S1|7" in sn._contexts, list(sn._contexts))
ev = [m for m in sn._ws.sent if m["method"] == "Runtime.evaluate"]
check("新 context 立即起播", ev and ev[0]["params"]["contextId"] == 7)
check("起播脚本带静音 play",
      "muted" in ev[0]["params"]["expression"]
      and "play()" in ev[0]["params"]["expression"])
check("起播命令落在正确 session", ev[0].get("sessionId") == "S1")

# 隔离世界(扩展)不该被记录
feed(sn, "Runtime.executionContextCreated",
     {"context": {"id": 9, "auxData": {"isDefault": False}}}, session="S1")
check("跳过隔离世界 context", "S1|9" not in sn._contexts)

sn._ws.sent.clear()
sn._poke_play()
evs = [m for m in sn._ws.sent if m["method"] == "Runtime.evaluate"]
check("定时器在每个 context 各跑一次", len(evs) == len(sn._contexts), evs)

feed(sn, "Runtime.executionContextDestroyed", {"executionContextId": 7},
     session="S1")
check("context 销毁后移除", "S1|7" not in sn._contexts)

print("=== 5. 视频地址过滤 ===")
hits = []
sn.captured.connect(hits.append)


def req(url, session=""):
    feed(sn, "Network.requestWillBeSent", {
        "request": {
            "url": url,
            "headers": {
                "Referer": "https://mxdrop.click/",
                "User-Agent": "UA/1.0",
                "Accept": "*/*",
            },
        },
        "type": "Media",
        "documentURL": "https://livecamrips.to/",
    }, session=session)


req("https://n1sxu2vld.mxcontent.net/v2/owwx3wmkig9z6q.mp4?s=x&e=1")
check("mp4 直链命中", len(hits) == 1, hits)
check("Referer 来自真实请求头",
      hits[0]["headers"].get("Referer") == "https://mxdrop.click/")
check("type 记录为 Media", hits[0]["type"] == "Media")

req("https://a.com/master.m3u8")
check("m3u8 命中", len(hits) == 2)

req("https://n1sxu2vld.mxcontent.net/v2/owwx3wmkig9z6q.mp4?s=x&e=1")
check("同一地址不重复", len(hits) == 2)

for bad in ("https://a.com/seg-1.ts",
            "https://a.com/x.m4s",
            "https://a.com/k.key",
            "https://a.com/a.vtt",
            "https://a.com/page.html",
            "blob:https://a.com/uuid"):
    req(bad)
check("ts/m4s/key/vtt/html/blob 都不算候选", len(hits) == 2, len(hits))

feed(sn, "Network.loadingFinished", {"requestId": "1"})
check("无关事件不影响结果", len(hits) == 2)

print("=== 6. 收尾 ===")
check("hits 属性条数一致", len(sn.hits) == 2, sn.hits)
sn.stop(close_browser=False)
check("stop 后未激活", sn.active is False)

print()
print(f"通过 {PASS} 项, 失败 {FAIL} 项")
sys.exit(0 if FAIL == 0 else 1)
