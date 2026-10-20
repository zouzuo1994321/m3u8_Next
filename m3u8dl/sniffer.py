"""网页视频嗅探 —— 复刻猫抓(cat-catch)核心能力。

输入网页地址后:
1. 抓取页面 HTML(浏览器 UA + Referer)
2. 正则识别页面内嵌的 m3u8 / mp4 / flv 直链(兼容 JSON 转义)
3. 对 m3u8 做 playlist 解析:
   - Master Playlist -> 逐个变体拉取, 列出多码率(清晰度/码率/时长/大小)
   - Media Playlist -> 单流信息(时长/分片数/直播判定/大小估算)
4. 输出结构化 VideoInfo 列表, 供界面展示与一键填充下载参数
"""
from __future__ import annotations

import html as html_mod
import re
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse

import requests

from . import mp4info, reqhdr
from .playlist import (
    MediaPlaylist,
    MasterPlaylist,
    format_duration,
    format_size,
    is_placeholder_url,
    parse_m3u8,
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 页面内视频直链正则(兼容 JSON 转义 \\/ 与 \\u002F)
_M3U8_RE = re.compile(r'https?://[^\s"\'<>\\)]+?\.m3u8(?:\?[^\s"\'<>\\)]*)?')
_MP4_RE = re.compile(r'https?://[^\s"\'<>\\)]+?\.mp4(?:\?[^\s"\'<>\\)]*)?')
_FLV_RE = re.compile(r'https?://[^\s"\'<>\\)]+?\.flv(?:\?[^\s"\'<>\\)]*)?')
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

MAX_VARIANTS_PROBED = 8


@dataclass
class VideoInfo:
    """一个可下载的视频条目。"""
    title: str = ""
    mime: str = ""
    resolution: str = ""
    bitrate_kbps: int = 0
    duration_text: str = ""
    size_text: str = ""
    url: str = ""
    is_live: bool = False
    source: str = ""                 # 来源页面
    variant_count: int = 0           # master 变体数量
    headers: dict = field(default_factory=dict)   # 下载所需请求头(Referer 等)
    #: DASH 音轨地址(B站一类音视频分离的源)。非空时下载器会分别下两条流,
    #: 再用 ffmpeg 合成 mp4; 为空则按普通 m3u8 / 直链处理。
    audio_url: str = ""

    @property
    def display_title(self) -> str:
        t = self.title or "未命名视频"
        if self.resolution:
            return f"{t} [{self.resolution}]"
        return t


class Sniffer:
    def __init__(self, timeout: int = 15, use_proxy: bool = True, log=None):
        self.timeout = timeout
        self.use_proxy = use_proxy
        self._log = log or (lambda msg: None)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_UA})
        if not use_proxy:
            self.session.trust_env = False
        # 浏览器引擎嗅探时回放的真实请求头(url -> headers)
        self._extra: dict = {}
        self._default_extra: dict = {}
        self._page_url = ""

    # ------------------------------------------------------------ 请求头处理
    def _extra_for(self, url: str) -> dict:
        """取该地址应回放的请求头(精确匹配 -> 同域匹配 -> 默认)。"""
        if url in self._extra:
            return self._extra[url]
        try:
            host = urlparse(url).hostname
        except Exception:
            host = None
        if host:
            for k, v in self._extra.items():
                try:
                    if urlparse(k).hostname == host:
                        return v
                except Exception:
                    continue
        return self._default_extra

    def _headers_for(self, url: str) -> dict:
        """组装请求头: 默认 Referer + 浏览器实际请求头。"""
        h = {}
        if self._page_url:
            h["Referer"] = self._page_url
        h.update(self._extra_for(url) or {})
        return {k: v for k, v in h.items() if v}

    # ------------------------------------------------------------------ 主入口
    def sniff(self, page_url: str) -> List[VideoInfo]:
        """嗅探页面(或直接解析 m3u8/mp4 直链), 返回视频信息列表。"""
        self._page_url = page_url or ""
        path = urlparse(page_url).path.lower()
        if path.endswith(".m3u8"):
            self._log("输入为 m3u8 直链, 直接解析")
            return self._probe_m3u8(page_url, page_url, "")
        if path.endswith((".mp4", ".flv")):
            self._log("输入为视频直链, 直接收录")
            return [self._probe_direct(page_url, page_url, "")]

        self._log(f"开始抓取页面: {page_url}")
        resp = self.session.get(page_url, timeout=self.timeout, allow_redirects=True)
        resp.raise_for_status()
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        page_html = resp.text
        page_title = self._extract_title(page_html)
        self._log(f"页面标题: {page_title or '(无)'}")

        candidates = self._extract_urls(page_html)
        self._log(f"发现候选地址 {len(candidates)} 个, 开始逐一解析…")

        infos: List[VideoInfo] = []
        seen = set()
        for url in candidates:
            if url in seen:
                continue
            seen.add(url)
            try:
                got = self._probe(url, page_url, page_title)
            except Exception as e:
                self._log(f"解析失败 {url[:90]}: {e}")
                continue
            infos.extend(got)
        return self._dedupe(infos)

    def sniff_urls(self, urls: List[str], page_url: str = "",
                   page_title: str = "",
                   headers_map: Optional[dict] = None) -> List[VideoInfo]:
        """直接解析一组候选视频地址(供 WebEngine 拦截结果使用)。

        与 sniff() 的区别: 不抓取页面 HTML, 直接对浏览器拦截到的地址做
        m3u8 解析 / 直链收录, 复用同一套探测逻辑。

        headers_map: {url: headers} —— 浏览器发该请求时携带的真实请求头
        (Referer / Origin / User-Agent / Cookie 等)。带防盗链的 CDN 必须
        原样回放, 否则会返回 403。
        """
        self._page_url = page_url or ""
        self._extra = dict(headers_map or {})
        self._default_extra = {}
        for v in self._extra.values():
            if v:
                self._default_extra = dict(v)
                break
        self._log(f"开始解析 {len(urls)} 个拦截地址…")
        infos: List[VideoInfo] = []
        seen = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            try:
                got = self._probe(url, page_url, page_title)
            except Exception as e:
                self._log(f"解析失败 {url[:90]}: {e}")
                continue
            infos.extend(got)
        return self._dedupe(infos)

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _dedupe(infos: List[VideoInfo]) -> List[VideoInfo]:
        """按地址去重：同一地址常被 master 变体与独立候选各收一次。

        重复条目里往往一个是「带清晰度/码率」的完整信息、另一个是走 Media
        分支的裸条目（分辨率与码率为空），保留信息更全的那条。
        """
        out: dict = {}
        for i in infos:
            old = out.get(i.url)
            if old is None:
                out[i.url] = i
                continue
            better = ((i.bitrate_kbps or 0) > (old.bitrate_kbps or 0)
                      or (i.resolution and not old.resolution)
                      or (i.duration_text and not old.duration_text))
            if better:
                out[i.url] = i
        return list(out.values())

    def _extract_title(self, page_html: str) -> str:
        m = _TITLE_RE.search(page_html)
        if not m:
            return ""
        title = html_mod.unescape(m.group(1))
        title = re.sub(r"\s+", " ", title).strip()
        return title[:80]

    def _extract_urls(self, page_html: str) -> List[str]:
        text = page_html.replace("\\/", "/").replace("\\u002F", "/")
        urls: List[str] = []
        for regex in (_M3U8_RE, _MP4_RE, _FLV_RE):
            for m in regex.finditer(text):
                u = m.group(0).rstrip("\\").rstrip("'\"")
                if u not in urls:
                    urls.append(u)
        return urls

    def _probe(self, url: str, page_url: str, page_title: str) -> List[VideoInfo]:
        path = urlparse(url).path.lower()
        if path.endswith(".m3u8"):
            return self._probe_m3u8(url, page_url, page_title)
        return [self._probe_direct(url, page_url, page_title)]

    def _probe_direct(self, url: str, page_url: str, page_title: str) -> VideoInfo:
        """mp4/flv 直链: 探测大小与 Range, 并从 moov 读分辨率/时长/码率。

        以前这里只发一个 HEAD 拿 Content-Length, 于是列表里分辨率、码率、时长
        全是"—"。用户看到全空会以为"根本没嗅探到"(实测 stripchat 系页面正是
        只下发 mp4 直链, 不下发 m3u8)。MP4 的信息其实都在 ``moov`` box 里,
        读头/尾各一小段即可, 代价 < 500KB。
        """
        name = urlparse(url).path.rsplit("/", 1)[-1] or "video"
        req_headers = self._headers_for(url)
        res = reqhdr.probe_media(self.session, url, req_headers,
                                 self._page_url or page_url, self.timeout)
        if res.ok:
            use = dict(res.headers or req_headers)
            total = res.total
            ctype = res.ctype
        else:
            use, total, ctype = dict(req_headers), 0, ""
            if res.error:
                self._log(f"直链探测未通过({url[:70]}): {res.error}")

        mime = ctype or ("video/mp4" if ".mp4" in url.lower()
                         else "video/x-flv")
        meta = {"duration": 0.0, "width": 0, "height": 0, "bitrate_kbps": 0}
        if res.ok and "mp4" in (url.lower() if ".mp4" in url.lower() else mime):
            meta = mp4info.probe_url(self.session, url, use, total, self.timeout)

        res_txt = (f"{meta['width']}x{meta['height']}"
                   if meta.get("width") and meta.get("height") else "")
        dur = meta.get("duration") or 0.0
        return VideoInfo(
            title=page_title or name,
            mime=mime,
            resolution=res_txt,
            bitrate_kbps=meta.get("bitrate_kbps") or 0,
            duration_text=format_duration(dur) if dur > 0 else "",
            size_text=format_size(total) if total else "未知",
            url=url,
            source=page_url,
            headers=use,
        )

    def _probe_m3u8(self, url: str, page_url: str, page_title: str) -> List[VideoInfo]:
        """解析 m3u8: master 展开变体, media 输出单流。

        请求头走 ``reqhdr.fetch_playlist`` 的 4 套策略回退 —— 站点对 Referer 的
        要求互相矛盾(有的要宿主页、有的要自身域、有的不能带), 只回放一套必然
        在部分站点上被 302 到占位流或 403。命中后把**实测可用的那套头**写进
        ``VideoInfo.headers``, 供下载与预览复用。
        """
        req_headers = self._headers_for(url)
        res = reqhdr.fetch_playlist(self.session, url, req_headers,
                                    self._page_url or page_url,
                                    self.timeout, self._log)
        if not res.ok:
            self._log(f"解析失败({url[:80]}): {reqhdr.describe_failure(res)}")
            return []

        text = res.text
        final = res.final_url or url
        # 关键: base 必须用**重定向后的最终地址**。
        # 实测某源站把未授权请求 302 到 taunt/promo 占位清单, 其变体是相对地址
        # ``index.m3u8``; 若仍用请求前地址做 base, 变体会解析回原地址, 上层就会
        # "master -> 变体 -> master" 死循环。
        if final != url:
            self._log(f"地址被重定向: {url[:70]} → {final[:70]}")
        if is_placeholder_url(final):
            self._log(f"跳过占位/广告流(该地址已失效): {final[:90]}")
            return []
        pl = parse_m3u8(text, final)
        # 后续变体/大小估算都沿用这套实测可用的头
        use_headers = dict(res.headers or req_headers)

        if isinstance(pl, MasterPlaylist):
            infos: List[VideoInfo] = []
            for v in pl.variants[:MAX_VARIANTS_PROBED]:
                dur, live, size = self._probe_variant(v.url, page_url,
                                                      use_headers)
                infos.append(VideoInfo(
                    title=page_title or "未命名视频",
                    mime="application/vnd.apple.mpegurl",
                    resolution=v.resolution,
                    bitrate_kbps=v.bitrate_kbps,
                    duration_text="直播中" if live else format_duration(dur),
                    size_text=size,
                    url=v.url,
                    is_live=live,
                    source=page_url,
                    variant_count=len(pl.variants),
                    headers=dict(use_headers),
                ))
            return infos

        # Media Playlist
        size = self._estimate_size(pl, final, use_headers)
        return [VideoInfo(
            title=page_title or "未命名视频",
            mime="application/vnd.apple.mpegurl",
            resolution="",
            bitrate_kbps=0,
            duration_text="直播中" if pl.is_live else format_duration(pl.duration),
            size_text=size,
            url=final,
            is_live=pl.is_live,
            source=page_url,
            headers=dict(use_headers),
        )]

    def _probe_variant(self, url: str, page_url: str, headers=None):
        """拉取变体 playlist, 返回 (时长, 是否直播, 大小文本)。"""
        res = reqhdr.fetch_playlist(self.session, url, headers or {},
                                    self._page_url or page_url,
                                    self.timeout, None)
        if not res.ok:
            return 0.0, False, "占位流" if "占位流" in (res.error or "") else "未知"
        pl = parse_m3u8(res.text, res.final_url or url)
        if isinstance(pl, MediaPlaylist):
            return (pl.duration, pl.is_live,
                    self._estimate_size(pl, res.final_url or url,
                                        res.headers or headers))
        return 0.0, False, "未知"

    def _estimate_size(self, pl: MediaPlaylist, url: str, headers=None) -> str:
        """大小估算: 首个分片 HEAD 大小 x 分片数(对齐猫抓的估算逻辑)。"""
        if not pl.segments:
            return "未知"
        if pl.is_live:
            return "直播"
        try:
            seg = pl.segments[0].url
            head = self.session.head(seg, timeout=self.timeout,
                                     headers=headers or self._headers_for(seg))
            if head.status_code == 200:
                first = int(head.headers.get("Content-Length", 0) or 0)
                if first > 0:
                    return f"≈{format_size(first * len(pl.segments))}"
        except Exception:
            pass
        return "未知"
