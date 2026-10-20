"""基于 QtWebEngine 的网页嗅探 —— 复刻猫抓(cat-catch)的网络请求拦截能力。

背景: 静态 HTML 正则只能抓到页面源码里写死的视频直链; 而大量站点
(Stripchat 类直播站、SPA 播放器等)的视频地址由 JS 动态构造, 经
XHR/fetch 在页面加载后延迟请求, 源码中并不存在——必须用真实浏览器
引擎加载页面并拦截其网络请求才能捕获。这正是猫抓 hook webRequest 的原理。

实现: 拦截器挂到 QWebEngineProfile.defaultProfile(), 对页面加载(及后续
预览)的所有请求生效; 命中视频特征的地址即时收集, 页面加载完成后再监听
一段"沉降时间"以捕获延迟加载的播放器请求。监听期间页面显示在预览区,
用户可与页面交互(如点击播放按钮), 与猫抓的使用体验一致。
"""
from __future__ import annotations

import re
import threading
from typing import List

from PySide6.QtCore import QObject, QTimer, Signal

from . import adblock

try:
    from PySide6.QtWebEngineCore import (
        QWebEngineUrlRequestInfo,
        QWebEngineUrlRequestInterceptor,
    )
    WEBENGINE_OK = True
except ImportError:  # pragma: no cover - 无 QtWebEngine 时降级
    WEBENGINE_OK = False


# ---------------------------------------------------------------- 地址特征
# 视频直链特征: .m3u8 清单(含查询串) / m3u8 路径参数 / .mp4 / .flv
_VIDEO_URL_RE = re.compile(
    r"\.m3u8(\?|#|$)|m3u8[=&/?]|\.mp4(\?|#|$)|\.flv(\?|#|$)",
    re.IGNORECASE,
)

# 明显与视频无关的静态资源/统计脚本, 命中即跳过
_SKIP_RE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|ico|css|js|mjs|woff2?|ttf|eot|otf|map)"
    r"(\?|#|$)"
    r"|google-analytics|googletagmanager|doubleclick|facebook\.net"
    r"|hotjar|sentry\.io|clarity\.ms",
    re.IGNORECASE,
)

# 媒体分片(AES key / 字幕等): 命中媒体类型也不收录, 避免直播分片刷屏
_SEGMENT_RE = re.compile(
    r"\.(ts|m4s|aac|mp3|key|vtt|srt)(\?|#|$)",
    re.IGNORECASE,
)

# 关键词特征: 部分站点的清单地址不带 .m3u8 扩展名(由后端接口下发),
# 只出现在 XHR/子资源请求里。命中这些关键词且资源类型是脚本型请求时收录。
_KEYWORD_RE = re.compile(r"m3u8|hls|playlist|manifest", re.IGNORECASE)

# 关键词收录只针对"数据型"请求(排除图片/样式/脚本等静态资源)
_KEYWORD_TYPES = None
try:
    from PySide6.QtWebEngineCore import QWebEngineUrlRequestInfo as _RT

    _T = _RT.ResourceType
    _KEYWORD_TYPES = {
        _T.ResourceTypeXhr,
        _T.ResourceTypeMedia,
        _T.ResourceTypeUnknown,
        _T.ResourceTypeSubResource,
        _T.ResourceTypeJson,
    }
except Exception:  # pragma: no cover
    _KEYWORD_TYPES = None


class VideoUrlInterceptor(QWebEngineUrlRequestInterceptor):
    """拦截页面网络请求, 收集其中的视频地址(不阻塞任何请求)。

    注意: interceptRequest 可能在 QtWebEngine 的 IO 线程被调用,
    收集操作必须加锁; hit 信号跨线程自动排队到 GUI 线程。
    """

    hit = Signal(str, str)     # (url, resource_type_name)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._urls: List[str] = []
        self._seen = set()
        self._meta = {}         # url -> {headers, referer, resource_type}
        self.active = False     # 仅在嗅探进行中收集
        # 广告拦截: 默认开启。只拦广告/统计请求, 视频地址永不拦截(见 interceptRequest)
        self.adblock = True
        self.blocked = 0

    def interceptRequest(self, info):
        try:
            if not self.active and not self.adblock:
                return
            url = info.requestUrl().toString()
            if not url.startswith(("http://", "https://")):
                return
            try:
                rtype = info.resourceType()
                rtype_name = str(rtype).rsplit(".", 1)[-1]
            except Exception:
                rtype = None
                rtype_name = ""

            # ① 视频地址: 先收集, 且**永不拦截**
            #    (广告联盟也会下发 m3u8, 拦掉就少了候选; 用户自己判断)
            if _VIDEO_URL_RE.search(url):
                if self.active:
                    self._collect(url, info, rtype_name)
                return

            # ② 广告/统计请求: 直接 block, 请求不出网
            if self.adblock and adblock.should_block(url, rtype_name):
                try:
                    info.block(True)
                    with self._lock:
                        self.blocked += 1
                except Exception:
                    pass
                return

            if not self.active:
                return
            if _SKIP_RE.search(url):
                return
            # 无扩展名但含 HLS 语义的清单地址(后端接口下发, 只在 XHR 中出现)
            if _KEYWORD_TYPES is not None and rtype in _KEYWORD_TYPES:
                if _KEYWORD_RE.search(url) and not _SEGMENT_RE.search(url):
                    self._collect(url, info, rtype_name)
                    return
            # 无扩展名的媒体流(部分 HLS 站点清单地址不带 .m3u8)
            if (rtype == QWebEngineUrlRequestInfo.ResourceTypeMedia
                    and not _SEGMENT_RE.search(url)):
                self._collect(url, info, rtype_name)
        except Exception:
            pass

    def _collect(self, url: str, info, rtype_name: str = ""):
        if not rtype_name:
            try:
                rtype_name = str(info.resourceType()).rsplit(".", 1)[-1]
            except Exception:
                rtype_name = "?"

        # 记录浏览器发出该请求时携带的真实请求头 —— 很多 CDN(如
        # streambucket.xyz)会校验 Referer/Origin/User-Agent 组合, 用通用头
        # 二次请求会被 403; 原样回放才能解析与下载。
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
            referer = info.firstPartyUrl().toString()
        except Exception:
            referer = ""

        with self._lock:
            if url in self._seen:
                return
            self._seen.add(url)
            self._urls.append(url)
            self._meta[url] = {
                "headers": headers,
                "referer": referer,
                "resource_type": rtype_name,
            }
        self.hit.emit(url, rtype_name)

    def clear(self):
        with self._lock:
            self._urls.clear()
            self._seen.clear()
            self._meta.clear()
            self.blocked = 0

    def take_items(self) -> List[dict]:
        """取出并清空已收集项(url + 该请求的真实请求头 + 来源页)。"""
        with self._lock:
            items = []
            for u in self._urls:
                meta = dict(self._meta.get(u) or {})
                meta["url"] = u
                items.append(meta)
            self._urls.clear()
            self._seen.clear()
            self._meta.clear()
        return items

    def take_urls(self) -> List[str]:
        """取出并清空已收集的地址列表。"""
        with self._lock:
            urls = list(self._urls)
            self._urls.clear()
            self._seen.clear()
        return urls


class WebSniffSession(QObject):
    """编排一次浏览器嗅探: 开始监听 -> 页面加载 -> 沉降监听 -> 汇总地址。

    必须在 GUI 线程使用(QtWebEngine 要求)。页面加载由调用方在预览视图上
    执行, 加载完成通过 notify_page_loaded 通知本会话。
    """

    page_loaded = Signal(bool)      # 页面加载完成(ok)
    finished = Signal(list)         # 监听结束, 候选项列表(url + 请求头 + 来源页)

    SETTLE_MS = 12000     # 页面加载完成后的沉降监听时长(捕获延迟加载的播放器)
    HARD_MS = 60000       # 硬超时(页面长时间不加载完时强制结束)

    def __init__(self, interceptor: "VideoUrlInterceptor", parent=None):
        super().__init__(parent)
        self._interceptor = interceptor
        self._running = False
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._on_settle)
        self._hard = QTimer(self)
        self._hard.setSingleShot(True)
        self._hard.timeout.connect(self._on_hard)

    @property
    def running(self) -> bool:
        return self._running

    def start(self):
        """开始监听(调用方需随后在预览视图加载目标页面)。"""
        self._interceptor.clear()
        self._interceptor.active = True
        self._running = True
        self._hard.start(self.HARD_MS)

    def notify_page_loaded(self, ok: bool):
        """页面加载完成回调(由预览视图的 loadFinished 驱动)。"""
        if not self._running:
            return
        self.page_loaded.emit(ok)
        if ok:
            self._settle.start(self.SETTLE_MS)
        else:
            self.stop()

    def stop(self):
        """结束监听并汇总已捕获地址(用户手动停止或沉降/超时到达)。"""
        if not self._running:
            return
        self._running = False
        self._interceptor.active = False
        self._settle.stop()
        self._hard.stop()
        self.finished.emit(self._interceptor.take_items())

    def _on_settle(self):
        self.stop()

    def _on_hard(self):
        self.stop()


def rank_candidate_items(items, limit: int = 40):
    """按可信度整理候选项(保留其请求头), 过滤静态资源/分片并去重限流。"""
    video_like, keyword_like, others = [], [], []
    seen = set()
    for it in items or []:
        u = it.get("url") if isinstance(it, dict) else it
        if not isinstance(u, str) or not u.startswith(("http://", "https://")):
            continue
        if u in seen:
            continue
        seen.add(u)
        if _SKIP_RE.search(u) or _SEGMENT_RE.search(u):
            continue
        item = it if isinstance(it, dict) else {"url": u}
        if _VIDEO_URL_RE.search(u):
            video_like.append(item)
        elif _KEYWORD_RE.search(u):
            keyword_like.append(item)
        else:
            others.append(item)
    return (video_like + keyword_like + others)[:limit]
