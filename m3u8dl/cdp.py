"""系统浏览器嗅探 —— 用 CDP 接管本机 Edge/Chrome。

为什么需要它
------------
内置的 QtWebEngine 是开源编译版, 有两个绕不过去的天然局限:

1. **没有 H.264/AAC 解码器**(专利), 页面内永远播不了这类视频;
2. 播放器在**跨域 iframe** 里时, ``runJavaScript`` 进不去, 只能靠注入脚本
   在子框架里主动 ``play()`` 去"逼"出请求 —— 有些站点仍然不买账。

而用户在真实浏览器里点一下播放, 所有请求都会正常发出(猫抓就是这么工作的)。
本模块通过 **Chrome DevTools Protocol** 连上系统浏览器, 只订阅
``Network.requestWillBeSent`` 事件, 不注入任何脚本、不影响页面:

    ▸ 启动 ``msedge.exe --remote-debugging-port=<port> --user-data-dir=<tmp>``
    ▸ 轮询 ``http://127.0.0.1:<port>/json/list`` 找到 page target
    ▸ ``QWebSocket`` 连上该 target, 发 ``Network.enable``
    ▸ 收事件, 过滤视频地址, 连同**浏览器真实发出的请求头**一起记下来

拿到的是真浏览器的真实 Referer/Origin/Cookie, 正好喂给下载器。

两个**必须**踩过的坑
--------------------
1. **OOPIF**: 跨域 iframe 在 Chromium 里是独立的 target(site isolation),
   它发出的网络请求**不经过顶层 session**, 只听顶层等于什么都看不到 ——
   而播放器几乎总在跨域 iframe 里。必须开
   ``Target.setAutoAttach{autoAttach, flatten:true}`` 并把每个子 target 的
   sessionId 带上做 ``Network.enable``; 之后所有命令/事件都按 sessionId 分流。
2. **preload="none"**: 播放器不点就不发请求, 见 ``_POKE_JS``。起播脚本要在
   **每个 session 的每个 executionContext** 里各跑一次。

注意
----
- 新版 Chrome/Edge 要求远程调试必须用**独立的 user-data-dir**, 所以这里用
  ``%TEMP%`` 下的固定目录; 复用同一目录可以让第二次启动快很多(保留 Cookie)。
- 只监听, 不发送任何 DOM 操作, 页面行为和用户手动打开完全一致。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tempfile
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtWebSockets import QWebSocket

#: 浏览器可执行文件的常见位置(按优先级)
_CANDIDATES = (
    ("Edge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ("Edge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ("Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ("Chrome",
     r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ("Chrome", os.path.expandvars(
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")),
    ("Chrome", os.path.expandvars(
        r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe")),
)

#: 命中即认为可能是视频
_VIDEO_RE = re.compile(
    r"\.m3u8(\?|#|$)|/hls/|\.mp4(\?|#|$)|\.flv(\?|#|$)|\.webm(\?|#|$)"
    r"|/master\.m3u8|/playlist\.m3u8",
    re.IGNORECASE,
)

#: 分片/密钥不当作候选(数量太多, 且没有独立下载价值)
_SKIP_RE = re.compile(
    r"\.ts(\?|#|$)|\.m4s(\?|#|$)|\.key(\?|#|$)|\.aac(\?|#|$)"
    r"|\.vtt(\?|#|$)|/seg[-_]?\d+",
    re.IGNORECASE,
)

#: 主动起播脚本。uqload / streamtape 这一族播放器是
#: ``<video preload="none">``, 不点播放按钮就**永远不发视频请求**;
#: 而播放器又住在一个跨域 iframe 里, 从外面进不去。
#: 这里通过 CDP 在**每个 execution context** 里各跑一次, 等于替用户点了播放。
#: 静音是必须的 —— 有声音的自动播放会被拦。
_POKE_JS = r"""
(function () {
  function poke() {
    try {
      var vs = document.querySelectorAll("video, audio");
      for (var i = 0; i < vs.length; i++) {
        var v = vs[i];
        try {
          if (v.readyState === 0 || v.paused) {
            v.muted = true;
            v.setAttribute("muted", "");
            v.setAttribute("autoplay", "");
            if (v.preload === "none" || !v.preload) { v.preload = "auto"; }
            var p = v.play();
            if (p && p.catch) { p.catch(function () {}); }
          }
        } catch (e) {}
      }
      var btns = document.querySelectorAll(
        ".vjs-big-play-button, .jw-icon-display, .vp-play, " +
        "[class*=big-play], [class*=play-button], [id*=playbutton]");
      for (var k = 0; k < btns.length && k < 3; k++) {
        try { if (btns[k].offsetParent !== null) { btns[k].click(); } } catch (e) {}
      }
      return vs.length;
    } catch (e) { return -1; }
  }
  return poke();
})()
"""


def find_browser() -> tuple:
    """返回 (显示名, 可执行文件路径); 找不到返回 ("", "")。"""
    for name, path in _CANDIDATES:
        if path and os.path.isfile(path):
            return name, path
    return "", ""


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class BrowserSniffer(QObject):
    """接管系统浏览器, 通过 CDP 收集视频请求。"""

    #: 抓到一个候选: {"url": str, "headers": dict, "type": str}
    captured = Signal(dict)
    #: 状态文案(直接显示到日志)
    status = Signal(str)
    #: 抓取结束(浏览器被关掉或用户停止)
    finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: Optional[subprocess.Popen] = None
        self._ws: Optional[QWebSocket] = None
        self._port = 0
        self._name = ""
        self._hits: Dict[str, dict] = {}
        self._msg_id = 0
        self._active = False
        self._tries = 0
        #: executionContext 记录: key = "sessionId|contextId" -> (session, id)
        #: iframe 有自己的 context, 且跨域 iframe 还另有自己的 session,
        #: 两者都要记住才能把起播脚本送进去。
        self._contexts: Dict[str, tuple] = {}

        self._poll = QTimer(self)
        self._poll.setInterval(350)
        self._poll.timeout.connect(self._probe_target)

        #: 主动起播: 播放器普遍是 <video preload="none">, 不点就不发请求。
        #: 注意要在**每个** execution context(主框架 + 各 iframe) 里各来一次,
        #: 因为播放器通常就住在跨域 iframe 里。
        self._poke = QTimer(self)
        self._poke.setInterval(2500)
        self._poke.timeout.connect(self._poke_play)

    # ------------------------------------------------------------ 对外接口
    @property
    def hits(self) -> List[dict]:
        return list(self._hits.values())

    @property
    def active(self) -> bool:
        return self._active

    def start(self, url: str) -> bool:
        """启动浏览器并开始监听。返回是否成功拉起进程。"""
        if self._active:
            return True
        name, exe = find_browser()
        if not exe:
            self.status.emit("未找到 Edge / Chrome, 无法使用系统浏览器嗅探")
            return False
        self._name = name
        self._port = _free_port()
        profile = os.path.join(tempfile.gettempdir(), "m3u8dl_cdp_profile")
        try:
            os.makedirs(profile, exist_ok=True)
        except Exception:
            profile = tempfile.mkdtemp(prefix="m3u8dl_cdp_")

        args = [
            exe,
            f"--remote-debugging-port={self._port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            # 让页面尽量自动起播(uqload 系是 preload=none, 不播就不发请求);
            # 用户手动点播放同样有效, 这只是省一步
            "--autoplay-policy=no-user-gesture-required",
            url or "about:blank",
        ]
        try:
            # CREATE_NO_WINDOW: 否则拉起 Edge 的瞬间会闪一个黑色控制台窗口
            from .fftools import NO_WINDOW

            self._proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW)
        except Exception as e:               # noqa: BLE001
            self.status.emit(f"启动 {name} 失败: {e}")
            return False

        self._active = True
        self._tries = 0
        self._hits.clear()
        self.status.emit(f"已启动 {name}, 请在浏览器里点击播放…")
        self._poll.start()
        return True

    def stop(self, close_browser: bool = False):
        """停止监听(可选关闭浏览器)并发出 finished。"""
        was = self._active
        self._active = False
        self._poll.stop()
        self._poke.stop()
        self._contexts.clear()
        if self._ws is not None:
            try:
                self._ws.close()
                self._ws.deleteLater()
            except Exception:
                pass
            self._ws = None
        if close_browser and self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        self._proc = None
        if was:
            self.status.emit(f"系统浏览器嗅探结束, 共 {len(self._hits)} 条候选")
            self.finished.emit()

    # ------------------------------------------------------------ 内部流程
    def _probe_target(self):
        """轮询 /json/list 找 page target。"""
        if not self._active:
            return
        self._tries += 1
        if self._tries > 50:                 # 约 17 秒
            self.status.emit("连接浏览器调试端口超时(可能被安全软件拦截)")
            self.stop()
            return
        try:
            import requests

            r = requests.get(f"http://127.0.0.1:{self._port}/json/list",
                             timeout=1.5)
            targets = r.json()
        except Exception:
            if self._proc is not None and self._proc.poll() is not None:
                self.status.emit("浏览器已退出, 停止监听")
                self.stop()
            return

        pages = [t for t in targets
                 if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not pages:
            return
        # 只接受已经导航到 http(s) 的 target —— 浏览器刚启动时会先有一个
        # about:blank, 连上它等于什么也抓不到。
        pages = [t for t in pages
                 if str(t.get("url", "")).startswith(("http://", "https://"))]
        if not pages:
            return
        self._poll.stop()
        self._connect(pages[0]["webSocketDebuggerUrl"])

    def _connect(self, ws_url: str):
        self._ws = QWebSocket()
        self._ws.connected.connect(self._on_open)
        self._ws.textMessageReceived.connect(self._on_message)
        self._ws.disconnected.connect(self._on_closed)
        self.status.emit("已连上浏览器调试通道, 等待视频请求…")
        self._ws.open(QUrl(ws_url))

    def _send(self, method: str, params: Optional[dict] = None,
              session: str = ""):
        if self._ws is None:
            return
        self._msg_id += 1
        payload = {"id": self._msg_id, "method": method,
                   "params": params or {}}
        if session:
            payload["sessionId"] = session
        try:
            self._ws.sendTextMessage(json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    def _on_open(self):
        self._send("Network.enable")
        self._send("Page.enable")
        # 打开 Runtime 才会下发 executionContextCreated, 这是进入 iframe 的钥匙
        self._send("Runtime.enable")
        # 跨域 iframe = 独立 OOPIF target, 不自动附加就收不到它的请求
        self._send("Target.setDiscoverTargets", {"discover": True})
        self._send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        })
        self._poke.start()

    def _enable_session(self, session: str):
        """对子 target(iframe) 单独开网络/运行时监听。"""
        self._send("Network.enable", session=session)
        self._send("Page.enable", session=session)
        self._send("Runtime.enable", session=session)

    def _poke_play(self):
        """在每个已知 execution context 里跑一次起播脚本。

        主框架 + 各 iframe(OOPIF 另有 session) 都要来一遍 ——
        播放器 99% 在跨域 iframe 里, 只跑主框架等于白跑。
        """
        if not self._contexts:
            # 还没收到 executionContextCreated, 退化为默认上下文试一次
            self._send("Runtime.evaluate",
                       {"expression": _POKE_JS, "silent": True})
            return
        for session, cid in list(self._contexts.values()):
            self._send("Runtime.evaluate", {
                "expression": _POKE_JS,
                "contextId": cid,
                "silent": True,
                "returnByValue": True,
            }, session=session)

    def _on_closed(self):
        if self._active:
            self.status.emit("浏览器调试通道已断开")
            self.stop()

    def _on_message(self, msg: str):
        try:
            data = json.loads(msg)
        except Exception:
            return
        method = data.get("method")
        session = data.get("sessionId") or ""

        if method == "Target.attachedToTarget":
            p = data.get("params") or {}
            sid = p.get("sessionId") or ""
            info = p.get("targetInfo") or {}
            if sid and info.get("type") in ("iframe", "page"):
                self._enable_session(sid)
                self._send("Runtime.runIfWaitingForDebugger", session=sid)
            return
        if method == "Target.detachedFromTarget":
            sid = (data.get("params") or {}).get("sessionId") or ""
            if sid:
                for k in [k for k, v in self._contexts.items() if v[0] == sid]:
                    self._contexts.pop(k, None)
            return

        if method == "Runtime.executionContextCreated":
            ctx = (data.get("params") or {}).get("context") or {}
            cid = ctx.get("id")
            # auxData.isDefault=False 的是扩展/脚本的隔离世界, 跳过
            aux = ctx.get("auxData") or {}
            if isinstance(cid, int) and aux.get("isDefault", True):
                self._contexts[f"{session}|{cid}"] = (session, cid)
                # 新框架出现(比如刚插进来的 iframe)立刻起播, 不等定时器
                self._send("Runtime.evaluate", {
                    "expression": _POKE_JS, "contextId": cid,
                    "silent": True}, session=session)
            return
        if method == "Runtime.executionContextDestroyed":
            cid = (data.get("params") or {}).get("executionContextId")
            self._contexts.pop(f"{session}|{cid}", None)
            return
        if method == "Runtime.executionContextsCleared":
            for k in [k for k, v in self._contexts.items() if v[0] == session]:
                self._contexts.pop(k, None)
            return
        if method != "Network.requestWillBeSent":
            return
        params = data.get("params") or {}
        req = params.get("request") or {}
        url = req.get("url") or ""
        if not url.lower().startswith(("http://", "https://")):
            return
        if not _VIDEO_RE.search(url) or _SKIP_RE.search(url):
            return
        if url in self._hits:
            return
        headers = {}
        for k, v in (req.get("headers") or {}).items():
            if isinstance(k, str) and isinstance(v, str):
                headers[k] = v
        # CDP 的 Referer 就在 headers 里; firstPartyUrl 用页面地址兜底
        self._hits[url] = {
            "url": url,
            "headers": headers,
            "referer": params.get("documentURL") or "",
            "type": params.get("type") or "",
        }
        self.captured.emit(self._hits[url])
