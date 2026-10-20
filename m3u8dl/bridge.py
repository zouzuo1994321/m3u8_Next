"""页面 -> Python 的数据回传桥（QWebChannel + base64）。

方案选型（踩坑记录）
--------------------
缓存捕捉要把播放器 ``appendBuffer`` 收到的**原始媒体字节**搬回 Python，
数据量可达数百 MB。逐个试过的通道：

1. **自定义 URL scheme ``m3u8dl://`` + QWebEngineUrlSchemeHandler**
   （``requestBody()`` 是 QIODevice，理论上零编码最理想）。
   实测：页面导航到该 scheme 可以，但页面内 ``fetch`` / ``XHR`` 一律被
   Chromium 拒绝——

   - ``Fetch API cannot load m3u8dl://... URL scheme "m3u8dl" is not supported.``
   - ``Cross origin requests are only supported for protocol schemes:
     chrome, chrome-extension, chrome-untrusted, data, http, https.``

   这是 Chromium 的 scheme 白名单，Qt 侧 flags 改不动。放弃。

2. **页面 POST 到本地 http://127.0.0.1:port**
   受混合内容 / Private Network Access 限制，https 站点场景不可靠。放弃。

3. **QWebChannel**（本模块采用）：Qt 官方页桥，走 ``qt.webChannelTransport``，
   不经过网络栈，没有 CORS / 混合内容问题。
   实测限制：**不支持 ArrayBuffer**（传过去是空 dict ``{}``），字符串正常。
   因此二进制用 base64 编码传输（+33% 体积），在页面侧手工编码避免
   ``String.fromCharCode(...arr)`` 的栈溢出。

为避免把几百 MB 全压在内存里，``capture.CaptureStore`` 收到块后**立即写临时文件**，
只保留每块的偏移量与「是否容器头」标记，合并时按偏移量切片。
"""
from __future__ import annotations

import base64
from typing import Callable, Optional

from PySide6.QtCore import QFile, QIODevice, QObject, Slot
from PySide6.QtWebChannel import QWebChannel

#: 暴露给页面的对象名：页面里 ``ch.objects.m3u8dl``
OBJECT_NAME = "m3u8dl"

#: Qt 内置的 QWebChannel 客户端脚本（随 Qt 一起安装，无需外网）
_QWC_RESOURCE = ":/qtwebchannel/qwebchannel.js"

_qwc_cache: Optional[str] = None


def qwebchannel_js() -> str:
    """读取 Qt 内置 qwebchannel.js 源码（用于注入页面）。"""
    global _qwc_cache
    if _qwc_cache is None:
        f = QFile(_QWC_RESOURCE)
        if f.open(QIODevice.OpenModeFlag.ReadOnly):
            _qwc_cache = bytes(f.readAll()).decode("utf-8")
            f.close()
        else:
            _qwc_cache = ""
    return _qwc_cache


class _BridgeObj(QObject):
    """页面可调用的槽。全部参数都是字符串/整数，避免 QVariant 转换歧义。"""

    def __init__(self, on_chunk: Callable[[str, str, int, bytes], None],
                 on_meta: Callable[[str, str, str], None],
                 on_rec: Optional[Callable[[str, str, bytes], None]] = None,
                 parent=None):
        super().__init__(parent)
        self._on_chunk = on_chunk
        self._on_meta = on_meta
        self._on_rec = on_rec or (lambda *a: None)

    @Slot(str, str, int, str)
    def chunk(self, sid: str, mime: str, index: int, b64: str) -> None:
        """页面回传一个媒体数据块（base64）。缓存捕捉用。"""
        try:
            data = base64.b64decode(b64, validate=False) if b64 else b""
        except Exception:
            data = b""
        if data:
            self._on_chunk(sid or "", mime or "", int(index), data)

    @Slot(str, str, str)
    def rec(self, sid: str, kind: str, b64: str) -> None:
        """页面回传录制分片（base64）。

        单独开一个槽而不是复用 ``chunk``: 录制是单条连续流，不能按 (mime,索引)
        分桶，也不能被 ``CaptureStore`` 的"丢弃重复容器头"逻辑处理——WebM 的
        EBML 头只出现在最前面，丢了就整段报废。
        """
        try:
            data = base64.b64decode(b64, validate=False) if b64 else b""
        except Exception:
            data = b""
        if data:
            self._on_rec(sid or "", kind or "", data)

    @Slot(str, str, str)
    def meta(self, sid: str, key: str, value: str) -> None:
        """页面回传元信息（标题、mime 列表等）。"""
        self._on_meta(sid or "", key or "", value or "")

    @Slot(str, str)
    def log(self, sid: str, message: str) -> None:
        """页面诊断日志（不进日志面板，仅用于排障）。"""
        self._on_meta(sid or "", "__log__", message or "")


class PageBridge:
    """把 QWebChannel 装到某个 page 上，并把页面回调转成 Python 回调。"""

    def __init__(self, on_chunk: Callable[[str, str, int, bytes], None],
                 on_meta: Optional[Callable[[str, str, str], None]] = None,
                 on_rec: Optional[Callable[[str, str, bytes], None]] = None):
        self._obj = _BridgeObj(on_chunk, on_meta or (lambda *a: None), on_rec)
        self._channel: Optional[QWebChannel] = None
        self._page = None

    def install(self, page) -> bool:
        try:
            self._page = page
            self._channel = QWebChannel(page)
            self._channel.registerObject(OBJECT_NAME, self._obj)
            page.setWebChannel(self._channel)
            return True
        except Exception:
            return False

    def inject_script(self) -> str:
        """返回需要注入页面的脚本：qwebchannel.js + 连接辅助函数。"""
        return (
            qwebchannel_js()
            + "\n;(function(){"
              "window.__M3U8DL_CH = null;"
              "function connect(){"
              "  if (window.QWebChannel && window.qt && qt.webChannelTransport) {"
              "    new QWebChannel(qt.webChannelTransport, function(ch){"
              "      window.__M3U8DL_CH = ch.objects." + OBJECT_NAME + ";"
              "      if (window.__M3U8DL_ON_CH_READY) {"
              "        try { window.__M3U8DL_ON_CH_READY(window.__M3U8DL_CH); } catch(e){}"
              "      }"
              "    });"
              "    return;"
              "  }"
              "  setTimeout(connect, 80);"
              "}"
              "connect();"
              "})();"
        )


# ---------------------------------------------------------------- base64 工具
#: 注入到页面的 base64 编码器（手写循环，避免大数组展开导致栈溢出）
B64_ENCODER_JS = r"""
function __m3u8dlB64(u8) {
  var T = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  var n = u8.length, i = 0, out = "", c1, c2, c3;
  for (; i + 2 < n; i += 3) {
    c1 = u8[i]; c2 = u8[i + 1]; c3 = u8[i + 2];
    out += T[c1 >> 2] + T[((c1 & 3) << 4) | (c2 >> 4)]
         + T[((c2 & 15) << 2) | (c3 >> 6)] + T[c3 & 63];
  }
  var rest = n - i;
  if (rest === 1) {
    c1 = u8[i];
    out += T[c1 >> 2] + T[(c1 & 3) << 4] + "==";
  } else if (rest === 2) {
    c1 = u8[i]; c2 = u8[i + 1];
    out += T[c1 >> 2] + T[((c1 & 3) << 4) | (c2 >> 4)]
         + T[(c2 & 15) << 2] + "=";
  }
  return out;
}
"""
