"""B站(bilibili)支持 —— 直链接口解析 + DASH 音视频双流合并。

为什么 B站需要单独一条通路(和一般 HLS 站点完全不同):

1. **音视频是两条独立的 DASH 流**。网页播放器请求的是
   ``...-1-100026.m4s``(视频)与 ``...-1-30280.m4s``(音频)两个**完整文件**,
   不是带音轨的 TS 分片。只下视频 → 成片没声音; 所以本模块交出的每条流都
   带一个 ``audio_url``, 由 :meth:`downloader.M3U8Downloader._run_dash` 分别
   下载后 ``ffmpeg -c copy`` 合成 mp4。
2. **官方接口有 WAF 风控**。裸 ``requests`` 直接 GET ``api.bilibili.com`` 会
   吃 **412**(返回一张 HTML 拦截页, 不是 JSON)。实测解法: 先请求
   ``/x/frontend/finger/spi`` 拿 ``buvid3``/``buvid4``, 自己补一个 ``_uuid``,
   并带上 ``Referer`` / ``Origin`` / ``Accept: application/json``, 之后
   ``view`` 与 ``playurl`` 就都通了 —— **不需要** curl_cffi 之类的 TLS 伪装。
3. **CDN 只认 bilibili 的 Referer**。视频地址落在 ``*.bilivideo.com`` /
   ``*.mcdn.bilivideo.cn``, 不带 Referer 一律 403。

画质上限由登录态决定: 未登录通常 480P(接口仍会列出 1080P 的
``accept_description``, 但 ``dash.video`` 里只给 360P/480P)。想要 1080P+
需要在「浏览器嗅探」页签里登录过 B站, 程序会带上该 profile 的 Cookie。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

API_SPI = "https://api.bilibili.com/x/frontend/finger/spi"
API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYURL = "https://api.bilibili.com/x/player/playurl"

HOME = "https://www.bilibili.com/"

#: 画质 id -> 名称(playurl 的 accept_quality 用这套码)
QN_NAMES = {
    127: "8K", 126: "杜比视界", 125: "HDR", 120: "4K", 116: "1080P60",
    112: "1080P+", 80: "1080P", 74: "720P60", 64: "720P", 32: "480P",
    16: "360P", 6: "240P",
}

#: 编码偏好: avc1 兼容性最好(mp4 直出、系统播放器都能放), 其次 hevc, 最后 av1
CODEC_ORDER = ("avc1", "hev1", "hvc1", "av01")

_BV_RE = re.compile(r"/video/(BV[0-9A-Za-z]{10})", re.I)
_AV_RE = re.compile(r"/video/av(\d+)", re.I)
#: 短链域名(需要先跟随跳转才能拿到 bvid)
_SHORT_HOSTS = ("b23.tv", "bili2233.cn")

#: 关键词 -> 更友好的说明(接口 code)
API_HINTS = {
    -400: "请求错误(链接里的稿件号可能不完整)",
    -403: "无权限访问该稿件",
    -404: "稿件不存在或已被删除",
    -352: "风控校验失败, 请稍后重试",
    62002: "稿件不可见(审核中/私密)",
    87007: "该清晰度需要大会员",
    87008: "该清晰度需要登录",
}


class BiliError(RuntimeError):
    """B站解析失败(带可读原因)。"""


@dataclass
class BiliStream:
    """一条可下载的 DASH 流(视频 + 配套音频)。"""
    label: str = ""                  # 例如 "720P · 1280x720 · avc1"
    qn: int = 0
    resolution: str = ""             # 1280x720
    codec: str = ""                  # avc1.640033
    bandwidth: int = 0               # bps
    video_url: str = ""
    audio_url: str = ""
    audio_label: str = ""            # 例如 "132kbps"
    duration_s: int = 0
    size_bytes: int = 0              # 估算值(按时长 × 码率)
    headers: dict = field(default_factory=dict)

    @property
    def bitrate_kbps(self) -> int:
        return int((self.bandwidth or 0) / 1000)

    @property
    def duration_text(self) -> str:
        s = int(self.duration_s or 0)
        return f"{s // 60:02d}:{s % 60:02d}"

    @property
    def size_text(self) -> str:
        b = self.size_bytes
        if b <= 0:
            return ""
        mb = b / 1024 / 1024
        return f"≈{mb:.2f} MB" if mb < 1024 else f"≈{mb / 1024:.2f} GB"


@dataclass
class BiliResult:
    """一个稿件(或其中一个分P)的解析结果。"""
    title: str = ""
    bvid: str = ""
    cid: int = 0
    page: int = 1
    page_title: str = ""
    duration_s: int = 0
    pages: int = 1                    # 稿件分P总数(多P才在标题里标 P 名)
    streams: List[BiliStream] = field(default_factory=list)
    logged_in: bool = False
    cookie_tip: str = ""

    @property
    def display_title(self) -> str:
        """列表里显示的标题: 单P稿件不带分P名(否则一长串重复), 多P才标。"""
        if self.pages > 1 and self.page_title:
            part = self.page_title
            if len(part) > 20:
                part = part[:20] + "…"
            return f"{self.title} - P{self.page} {part}"
        return self.title


# --------------------------------------------------------------------- 地址
def is_bili_url(url: str) -> bool:
    """地址是否指向 B站视频页(含 b23.tv 短链)。"""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in _SHORT_HOSTS:
        return True
    return (host == "bilibili.com" or host.endswith(".bilibili.com")
            or host == "bilibili.tv" or host.endswith(".bilibili.tv"))


def parse_target(url: str) -> Optional[Tuple[str, str, int]]:
    """从视频页地址解析 ``(类型, 值, 分P序号)``。

    类型为 ``"bvid"`` 或 ``"aid"``; 解析不出返回 None(短链要先
    :func:`resolve_short` 跟随跳转)。
    """
    u = url or ""
    try:
        q = parse_qs(urlparse(u).query)
    except Exception:
        q = {}
    page = 1
    if q.get("p"):
        try:
            page = max(1, int(q["p"][0]))
        except (TypeError, ValueError):
            page = 1
    m = _BV_RE.search(u)
    if m:
        return "bvid", m.group(1), page
    m = _AV_RE.search(u)
    if m:
        return "aid", m.group(1), page
    return None


def resolve_short(url: str, timeout: int = 15,
                  session: Optional[requests.Session] = None) -> str:
    """b23.tv 短链跟随跳转, 返回最终的视频页地址。"""
    s = session or requests.Session()
    try:
        r = s.get(url, headers={"User-Agent": DEFAULT_UA, "Referer": HOME},
                  timeout=timeout, allow_redirects=True)
        return r.url or url
    except Exception as e:                     # noqa: BLE001 - 网络错误统一兜底
        raise BiliError(f"短链解析失败: {type(e).__name__} {e}") from e


# --------------------------------------------------------------------- 会话
def make_session(cookie: str = "", use_proxy: bool = True,
                 timeout: int = 15) -> requests.Session:
    """建一个"已经过完风控"的会话: 带浏览器头 + buvid Cookie。

    412 的根因就是缺 ``buvid3``/``buvid4`` —— 这一步不做, 后面所有接口都是
    一张 HTML 拦截页。
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": DEFAULT_UA,
        "Referer": HOME,
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    if not use_proxy:
        s.trust_env = False
    _apply_cookie(s, cookie)
    _bootstrap_cookies(s, timeout)
    return s


def _apply_cookie(session: requests.Session, cookie: str) -> None:
    """把 ``k=v; k2=v2`` 形式的 Cookie 串灌进会话(空则忽略)。"""
    if not cookie:
        return
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        try:
            session.cookies.set(k, v, domain=".bilibili.com")
        except Exception:                      # noqa: BLE001 - 个别非法键跳过
            pass


def _bootstrap_cookies(session: requests.Session, timeout: int = 15) -> None:
    """补 buvid3/buvid4/_uuid(拿不到不致命, 后续接口会给出可读错误)。"""
    try:
        r = session.get(API_SPI, timeout=timeout)
        data = (r.json() or {}).get("data") or {}
        if data.get("b_3"):
            session.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
        if data.get("b_4"):
            session.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")
    except Exception:                          # noqa: BLE001 - 尽力而为
        pass
    if not session.cookies.get("_uuid"):
        try:
            session.cookies.set("_uuid", str(uuid.uuid4()).upper() + "infoc",
                                domain=".bilibili.com")
        except Exception:                      # noqa: BLE001
            pass


def _api_json(session: requests.Session, api: str, params: dict,
              timeout: int = 15) -> dict:
    """GET 一个 api.bilibili.com 接口, 统一处理 412/非 JSON/code!=0。"""
    try:
        r = session.get(api, params=params, timeout=timeout)
    except Exception as e:                     # noqa: BLE001 - 网络异常
        raise BiliError(f"接口请求失败: {type(e).__name__} {e}") from e
    if r.status_code == 412:
        raise BiliError("B站风控拦截(412): 请稍后重试, 或先在「浏览器嗅探」"
                        "页签打开一次 B站 以带上 Cookie")
    ctype = (r.headers.get("content-type") or "").lower()
    if "json" not in ctype:
        raise BiliError(f"接口返回非 JSON(HTTP {r.status_code}): "
                        "多半是被风控拦截了")
    try:
        data = r.json()
    except ValueError as e:
        raise BiliError(f"接口返回无法解析: {e}") from e
    code = data.get("code", 0)
    if code != 0:
        hint = API_HINTS.get(code, data.get("message") or "未知错误")
        raise BiliError(f"接口错误 {code}: {hint}")
    return data.get("data") or {}


# --------------------------------------------------------------------- 解析
def _pick_codec(items: List[dict]) -> dict:
    """同一画质有多种编码时, 按 avc1 > hevc > av1 选一个。"""
    def rank(it: dict) -> tuple:
        codec = (it.get("codecs") or "").lower()
        for i, pre in enumerate(CODEC_ORDER):
            if codec.startswith(pre):
                return (i, -(it.get("bandwidth") or 0))
        return (len(CODEC_ORDER), -(it.get("bandwidth") or 0))
    return sorted(items, key=rank)[0]


def _best_audio(audios: List[dict]) -> dict:
    """音轨取带宽最高的(通常 30280 = 192kbps 以上)。"""
    return max(audios, key=lambda a: (a.get("bandwidth") or 0)) if audios else {}


def build_streams(dash: dict, duration_s: int) -> List[BiliStream]:
    """把 playurl 的 ``dash`` 结构整理成「视频流 + 配套音频」的候选列表。"""
    videos = [v for v in (dash.get("video") or []) if v]
    audios = [a for a in (dash.get("audio") or []) if a]
    if not videos:
        return []
    best_audio = _best_audio(audios)
    a_url = best_audio.get("baseUrl") or best_audio.get("base_url") or ""
    a_bw = best_audio.get("bandwidth") or 0
    a_label = f"{int(a_bw / 1000)}kbps" if a_bw else ""

    # 同一画质挑一个编码; 不同画质各留一条, 让用户自己选
    by_qn: Dict[int, List[dict]] = {}
    for v in videos:
        by_qn.setdefault(int(v.get("id") or 0), []).append(v)

    out: List[BiliStream] = []
    for qn in sorted(by_qn, reverse=True):
        v = _pick_codec(by_qn[qn])
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        res = f"{w}x{h}" if w and h else ""
        codec = (v.get("codecs") or "").split(".")[0]
        qn_name = QN_NAMES.get(qn, f"qn{qn}")
        label = " · ".join(x for x in (qn_name, res, codec) if x)
        bw = int(v.get("bandwidth") or 0)
        size = int((bw + a_bw) * (duration_s or 0) / 8)
        out.append(BiliStream(
            label=label, qn=qn, resolution=res, codec=v.get("codecs") or "",
            bandwidth=bw,
            video_url=v.get("baseUrl") or v.get("base_url") or "",
            audio_url=a_url, audio_label=a_label, duration_s=duration_s,
            size_bytes=size,
            headers={"Referer": HOME, "User-Agent": DEFAULT_UA},
        ))
    return out


def build_durl_streams(durl: List[dict], duration_s: int) -> List[BiliStream]:
    """老接口/番剧走 ``durl``(整段 mp4, 自带音轨)时的兜底。"""
    out = []
    for i, item in enumerate(durl or [], 1):
        url = item.get("url") or ""
        if not url:
            continue
        size = int(item.get("size") or 0) or int(
            (item.get("bandwidth") or 0) * (duration_s or 0) / 8)
        label = f"MP4 · 第{i}段" if len(durl) > 1 else "MP4"
        out.append(BiliStream(
            label=label, qn=0, resolution="", codec="", bandwidth=0,
            video_url=url, audio_url="", duration_s=duration_s,
            size_bytes=size,
            headers={"Referer": HOME, "User-Agent": DEFAULT_UA},
        ))
    return out


def fetch_view(session: requests.Session, bvid: str = "", aid: str = "",
               timeout: int = 15) -> dict:
    params = {"bvid": bvid} if bvid else {"aid": aid}
    return _api_json(session, API_VIEW, params, timeout)


def fetch_playurl(session: requests.Session, bvid: str, cid: int,
                  qn: int = 127, timeout: int = 15) -> dict:
    """取播放地址。``fnval=4048`` = DASH + 4K/HDR/杜比全家桶。"""
    params = {
        "bvid": bvid, "cid": cid, "qn": qn, "fnval": 4048, "fnver": 0,
        "fourk": 1, "platform": "pc",
    }
    return _api_json(session, API_PLAYURL, params, timeout)


def resolve(url: str, cookie: str = "", timeout: int = 15,
            use_proxy: bool = True, log=None) -> BiliResult:
    """解析一个 B站视频页地址, 返回可选清晰度列表。

    ``cookie`` 可以是 ``SESSDATA=...; bili_jct=...`` 这样的串(来自程序内置
    浏览器的登录态), 用来突破"未登录最高 480P"的限制。
    """
    say = log or (lambda m: None)
    if not is_bili_url(url):
        raise BiliError("不是 B站地址")

    session = make_session(cookie=cookie, use_proxy=use_proxy, timeout=timeout)
    target_url = url
    if not parse_target(url):
        # 短链(或不带 BV 号的专题页): 先跟随跳转再解析
        target_url = resolve_short(url, timeout=timeout, session=session)
        say(f"短链跳转: {target_url[:100]}")
    parsed = parse_target(target_url)
    if not parsed:
        raise BiliError("没能从地址里认出 BV 号/av 号, 请确认是视频播放页")

    kind, value, page = parsed
    bvid, aid = (value, "") if kind == "bvid" else ("", value)
    info = fetch_view(session, bvid=bvid, aid=aid, timeout=timeout)
    bvid = info.get("bvid") or bvid
    pages = info.get("pages") or []
    if not pages:
        raise BiliError("稿件里没有分P信息")
    idx = min(max(page, 1), len(pages)) - 1
    page_info = pages[idx]
    cid = int(page_info.get("cid") or 0)
    duration_s = int(page_info.get("duration") or info.get("duration") or 0)

    logged_in = bool(session.cookies.get("SESSDATA"))
    say(f"稿件: {info.get('title', '')[:40]} · P{page}/{len(pages)} · "
        f"时长 {duration_s}s · {'已登录' if logged_in else '未登录'}")

    data = fetch_playurl(session, bvid, cid, timeout=timeout)
    dash = data.get("dash") or {}
    streams = build_streams(dash, duration_s)
    if not streams:
        streams = build_durl_streams(data.get("durl") or [], duration_s)
    if not streams:
        raise BiliError("接口没有返回可下载的视频流(可能是付费/地区限制)")

    tip = ""
    if not logged_in:
        top = max((s.qn for s in streams), default=0)
        if top < 80:
            tip = "未登录最高 480P; 想要 1080P+ 请先在「浏览器嗅探」页签登录 B站"

    return BiliResult(
        title=info.get("title") or "", bvid=bvid, cid=cid, page=page,
        page_title=page_info.get("part") or "", duration_s=duration_s,
        pages=len(pages), streams=streams, logged_in=logged_in,
        cookie_tip=tip,
    )
