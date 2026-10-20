"""请求头策略与「带回退的清单拉取」。

为什么需要这一层
----------------
不同 CDN 校验 Referer 的规则**互相矛盾**, 没有一套头能通吃:

============================  ==================================================
站点行为                       实测结论
============================  ==================================================
``streambucket.xyz`` 类        必须回放**宿主页** Referer, 缺了直接 403
``uqload.vc`` 类                Referer 必须是**它自己的域**; 带宿主页 Referer
                               会被 **302 到 ``/taunt/promo/master.m3u8``**
                               (占位/钓鱼清单); 而**完全不带 Referer 反而 200**
============================  ==================================================

其中 uqload 那条是 v1.1.9 排查出来的真凶: 浏览器里的播放器在 ``uqload.vc`` 域内
发 XHR, 真实 Referer 是 ``https://uqload.vc/``; 而本软件把它换成了宿主页地址,
于是被判为非授权 → 302 → 被当成"token 失效的占位流"丢掉。**嗅探拿不到、预览
打不开、下载报占位流, 全是这一处造成的。**

另外实测该 CDN 还有**概率性 TLS 断连**(``SSL: UNEXPECTED_EOF_WHILE_READING``),
8 次里约 1~2 次失败, 与请求头无关 —— 必须带重试, 否则偶发失败会被误判成
"这个地址没用了"。

本模块的做法: 按 5 套策略依次试探, 命中即返回**这一套成功的请求头**, 让调用方
把它传给后续分片请求(否则嗅探成功、下载又挂)。

第三个坑(2026-10-20 实测): 部分 CDN 会做**请求头集合指纹**校验 —— 不只看
Referer/Origin, 而是"多带一个它没见过的头就 403"。``streambucket.xyz`` 就是
这样: 只要请求里出现 ``Accept-Language``, 无论值是 ``zh-CN`` 还是 ``en-US``,
一律 403; 去掉就 200。浏览器那条 XHR 本身并不带这个头, 是我们自己写死的默认
头把它带进去的。所以必须有一档 **``minimal``**: 只保留防盗链真正会看的
``User-Agent / Referer / Origin``(以及鉴权用的 ``Authorization / Cookie / X-*``),
其余一概不带。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .playlist import is_placeholder_url

#: 概率性 TLS/连接错误: 换一次连接大概率就好, 值得原地重试
_RETRY_ERRORS = (
    "SSLError",
    "SSLEOFError",
    "ConnectionError",
    "ConnectTimeout",
    "ReadTimeout",
    "ChunkedEncodingError",
    "ProtocolError",
)

#: 同一条 URL 的重试次数(仅针对上述瞬断错误)
DEFAULT_RETRIES = 3


@dataclass
class FetchResult:
    """一次「清单拉取」的结果, 含实际可用的请求头。"""
    ok: bool = False
    text: str = ""
    final_url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    variant: str = ""            # 生效的策略名
    status: int = 0
    retries: int = 0             # 因瞬断错误重试了几次
    error: str = ""

    @property
    def redirect_only(self) -> bool:
        """地址被重定向到了占位流(但 HTTP 层面是成功的)。"""
        return bool(self.final_url) and is_placeholder_url(self.final_url)


def _strip_empty(h: Optional[Dict[str, str]]) -> Dict[str, str]:
    return {k: v for k, v in (h or {}).items() if k and v}


def _origin_of(url: str) -> str:
    try:
        p = urlparse(url)
    except Exception:
        return ""
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


#: 精简档保留的头。防盗链真正会校验的就是这几个; 其余(Accept / Accept-Language
#: / sec-ch-ua / Accept-Encoding ...)都可能成为"多余头"触发头集合指纹校验。
_MINIMAL_KEEP = ("user-agent", "referer", "origin", "authorization", "cookie")

#: 噪声头: 会触发头集合指纹校验, 且对拉清单/分片毫无用处。如果它藏在
#: ``session.headers`` 里, requests 会把它合并进每一次请求 —— 那时连
#: ``minimal`` 档都救不回来, 所以进 fetch 前必须先从 session 上摘掉。
_NOISE_HEADERS = ("accept-language",)


def minimal_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    """只留防盗链必需的头(外加 x-* 自定义头), 丢掉其余一切。"""
    out: Dict[str, str] = {}
    for k, v in _strip_empty(headers).items():
        lk = k.lower()
        if lk in _MINIMAL_KEEP or lk.startswith("x-"):
            out[k] = v
    return out


def build_variants(url: str, headers: Optional[Dict[str, str]] = None,
                   page_url: str = "") -> List[Tuple[str, Dict[str, str]]]:
    """生成候选请求头策略(按优先级), 已按「Referer+Origin+头集合」去重。

    - ``captured``   浏览器捕获到的原样头(最可信, 含播放器真实 Referer)
    - ``minimal``    只留 UA/Referer/Origin/鉴权头(对付"多头即 403"的指纹校验)
    - ``site_root``  Referer/Origin 换成**目标站自己的**根地址(uqload 类)
    - ``no_referer`` 去掉 Referer/Origin(带任何外来 Referer 都会被拒的站点)
    - ``page``       Referer/Origin 换回**宿主页**(streambucket 类)
    """
    base = _strip_empty(headers)
    site = _origin_of(url)
    out: List[Tuple[str, Dict[str, str]]] = [("captured", dict(base))]

    def _key(h: Dict[str, str]):
        """去重键: 同一组 Referer/Origin 但头集合不同, 不能算重复。"""
        return ((h.get("Referer") or "").lower(),
                (h.get("Origin") or "").lower(),
                frozenset(k.lower() for k in h))

    seen = {_key(base)}

    mini = minimal_headers(base)
    if mini:
        k = _key(mini)
        if k not in seen:
            out.append(("minimal", mini))
            seen.add(k)

    if site:
        h = dict(mini or base)
        h["Referer"] = site + "/"
        h["Origin"] = site
        k = _key(h)
        if k not in seen:
            out.append(("site_root", h))
            seen.add(k)

    h = {k2: v for k2, v in (mini or base).items()
         if k2.lower() not in ("referer", "origin")}
    k = _key(h)
    if k not in seen:
        out.append(("no_referer", h))
        seen.add(k)

    page_site = _origin_of(page_url)
    if page_site:
        h = dict(mini or base)
        h["Referer"] = page_url
        h["Origin"] = page_site
        k = _key(h)
        if k not in seen:
            out.append(("page", h))
    return out


def _is_retryable(exc: BaseException) -> bool:
    name = type(exc).__name__
    return name in _RETRY_ERRORS or "SSL" in name


def fetch_playlist(session, url: str,
                   headers: Optional[Dict[str, str]] = None,
                   page_url: str = "",
                   timeout: float = 15,
                   log=None,
                   retries: int = DEFAULT_RETRIES) -> FetchResult:
    """拉取 m3u8 清单, 自动在 4 套请求头之间回退。

    成功判据: HTTP 200 且正文含 ``#EXTM3U``, 且最终地址不是占位流。
    返回的 ``headers`` 是**实测可用**的那一套, 调用方应转交给分片下载/预览。
    """
    import time as _time

    say = log or (lambda m: None)
    last = FetchResult()

    # requests 会把 session.headers 与本次传入的头**合并**, 所以藏在 session
    # 上的噪声头(典型: Accept-Language)会让每一档都带上它 —— 连 minimal 都
    # 救不回来。这里只删不增, 幂等且不会影响别的请求语义。
    try:
        for k in list(session.headers.keys()):
            if str(k).lower() in _NOISE_HEADERS:
                del session.headers[k]
    except Exception:                       # noqa: BLE001 - 会话实现差异
        pass

    for name, h in build_variants(url, headers, page_url):
        n_retry = 0
        for attempt in range(max(1, retries)):
            if attempt:
                n_retry += 1
                _time.sleep(0.5 * attempt)
            try:
                r = session.get(url, headers=h, timeout=timeout,
                                allow_redirects=True)
            except Exception as e:            # noqa: BLE001 - 保留分类判断
                last = FetchResult(
                    ok=False, headers=h, variant=name, retries=n_retry,
                    error=f"{type(e).__name__}: {e}")
                if _is_retryable(e) and attempt < retries - 1:
                    continue
                break
            final = getattr(r, "url", "") or url
            text = r.text or ""
            if r.status_code == 200 and "#EXTM3U" in text:
                if is_placeholder_url(final):
                    # 这套头被重定向到了占位流: 换下一套(referer 策略问题)
                    last = FetchResult(
                        ok=False, text=text, final_url=final, headers=h,
                        variant=name, status=r.status_code,
                        retries=n_retry,
                        error="被重定向到占位流")
                    break
                if name != "captured":
                    say(f"请求头策略修正为「{name}」(默认请求头被站点拒绝)")
                return FetchResult(ok=True, text=text, final_url=final,
                                   headers=h, variant=name,
                                   status=r.status_code, retries=n_retry)
            last = FetchResult(
                ok=False, text=text if r.status_code == 200 else "",
                final_url=final, headers=h, variant=name,
                status=r.status_code, retries=n_retry,
                error=f"HTTP {r.status_code}")
            if is_placeholder_url(final):
                last.error = "被重定向到占位流"
            if r.status_code in (404, 410):
                # 地址本身已不存在, 换 Referer 也救不回来 —— 直接收手,
                # 免得一页 9 个候选每个都白试 4 套策略(嗅探会慢到不可用)
                return last
            break

    if last.status or last.error:
        say(f"清单拉取失败({url[:80]}): {last.error or last.status}")
    return last


def describe_failure(res: FetchResult) -> str:
    """把 FetchResult 的失败原因翻成用户能照着做的提示。"""
    if res.ok:
        return ""
    if res.redirect_only or "占位流" in (res.error or ""):
        return ("地址被重定向到占位流(taunt/promo)。这通常意味着该地址已失效, "
                "请在「浏览器嗅探」页签里重新播放一次, 再解析新捕获的地址")
    if res.status == 403:
        return "站点返回 403(防盗链校验未通过), 已尝试 4 套 Referer 策略均被拒"
    if res.status == 404:
        return "站点返回 404, 该地址可能已过期"
    if res.error:
        return f"请求失败: {res.error}"
    return "无有效响应"


# --------------------------------------------------------------- 直链探测
@dataclass
class MediaProbe:
    """一次「视频文件直链」探测的结果(mp4/flv/webm...)。"""
    ok: bool = False
    variant: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    total: int = 0                  # 总字节数, 0 = 未知
    ranges: bool = False            # 是否支持 Range 分段
    ctype: str = ""                 # Content-Type(已去掉 ;charset)
    status: int = 0
    error: str = ""


def probe_media(session, url: str,
                headers: Optional[Dict[str, str]] = None,
                page_url: str = "",
                timeout: float = 20) -> MediaProbe:
    """探测视频**文件直链**(不是 m3u8): 总大小 / 是否支持 Range / 类型。

    只发 ``Range: bytes=0-0`` 取 1 字节 —— 直链动辄上百 MB, 不能真拉。
    与 :func:`fetch_playlist` 一样按多套请求头回退: 实测 stripchat 系 mp4
    要求 Referer/Origin 是**播放器 iframe 自身域**(如 ``mxdrop.click``),
    换成宿主页立刻 403。
    """
    last = MediaProbe()
    for name, h in build_variants(url, headers, page_url):
        hh = dict(h)
        hh["Range"] = "bytes=0-0"
        try:
            r = session.get(url, headers=hh, timeout=timeout,
                            allow_redirects=True, stream=True)
        except Exception as e:              # noqa: BLE001 - 交给上层决定
            last = MediaProbe(variant=name, headers=h,
                              error=f"{type(e).__name__}: {e}")
            continue
        try:
            ctype = (r.headers.get("Content-Type")
                     or "").lower().split(";")[0].strip()
            if r.status_code not in (200, 206):
                last = MediaProbe(variant=name, headers=h,
                                  status=r.status_code,
                                  error=f"HTTP {r.status_code}")
                continue
            if "text/html" in ctype:
                last = MediaProbe(variant=name, headers=h,
                                  status=r.status_code,
                                  error="返回 HTML(地址已失效或被防盗链拦截)")
                continue
            total = 0
            crange = r.headers.get("Content-Range") or ""
            if "/" in crange:
                try:
                    total = int(crange.rsplit("/", 1)[1])
                except ValueError:
                    total = 0
            if not total:
                try:
                    total = int(r.headers.get("Content-Length", 0) or 0)
                except ValueError:
                    total = 0
            ok_range = (r.status_code == 206 or "bytes" in (
                r.headers.get("Accept-Ranges") or "").lower())
            return MediaProbe(ok=True, variant=name, headers=dict(h),
                              total=total, ranges=ok_range, ctype=ctype,
                              status=r.status_code)
        finally:
            r.close()
    return last
