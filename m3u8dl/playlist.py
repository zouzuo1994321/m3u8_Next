"""m3u8 播放列表解析(对齐 HLS RFC 8216 与 N_m3u8DL-CLI 行为)。

支持:
- Master Playlist(#EXT-X-STREAM-INF 多码率变体)
- Media Playlist(分片列表 / EXTINF 时长 / AES-128 KEY / 直播判定)
- 属性解析(支持引号包裹的 CODECS 等值)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Union
from urllib.parse import urljoin, urlparse


@dataclass
class Variant:
    """Master Playlist 中的一个码率变体。"""
    url: str
    bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""
    name: str = ""

    @property
    def bitrate_kbps(self) -> int:
        return self.bandwidth // 1000 if self.bandwidth else 0

    @property
    def label(self) -> str:
        parts = []
        if self.resolution:
            parts.append(self.resolution)
        if self.bitrate_kbps:
            parts.append(f"{self.bitrate_kbps}kbps")
        return " / ".join(parts) if parts else (self.name or "未知")


@dataclass
class Segment:
    """Media Playlist 中的一个分片。"""
    url: str
    duration: float = 0.0
    index: int = 0
    title: str = ""


@dataclass
class KeyInfo:
    """EXT-X-KEY 加密信息(当前支持 AES-128)。"""
    method: str = "NONE"
    uri: str = ""
    iv: str = ""
    keyformat: str = ""


@dataclass
class MediaPlaylist:
    segments: List[Segment] = field(default_factory=list)
    key: Optional[KeyInfo] = None
    init_url: str = ""            # EXT-X-MAP(fMP4 init 分片)
    duration: float = 0.0
    target_duration: float = 0.0
    is_live: bool = True          # 无 #EXT-X-ENDLIST 视为直播
    media_sequence: int = 0


@dataclass
class MasterPlaylist:
    variants: List[Variant] = field(default_factory=list)


Playlist = Union[MasterPlaylist, MediaPlaylist]

_ATTR_RE = re.compile(r'([A-Za-z0-9-]+)=("([^"]*)"|[^,]*)')
_EXTINF_RE = re.compile(r"#EXTINF:([\d.]+)")


def parse_attrs(text: str) -> dict:
    """解析 #EXT-X-STREAM-INF / #EXT-X-KEY 等标签属性, 支持引号值。"""
    attrs = {}
    for m in _ATTR_RE.finditer(text):
        key = m.group(1).upper()
        val = m.group(3) if m.group(3) is not None else m.group(2)
        attrs[key] = val
    return attrs


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


#: 占位/广告流的路径特征。uqload / streamtape 这类网盘型源站, 在请求
#: **未获授权**(缺 Cookie、token 过期、Referer 不对)时不会返回 403, 而是
#: 302 到一个"钓鱼"清单(路径含 taunt/promo)。该清单本身是合法 m3u8:
#:
#:   #EXTM3U
#:   #EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=1280x720
#:   index.m3u8
#:
#: 变体是**相对地址**且与请求地址同名 —— 用请求前的地址做 base 解析, 变体
#: 就会指回原地址, 于是"master -> 变体 -> master"死循环, 最终报出完全误导
#: 的"无法解析 m3u8 播放列表"。
_PLACEHOLDER_HINTS = ("/taunt/", "/promo/", "/placeholder/", "/placeholders/",
                      "/promo.", "/taunt.")


def is_placeholder_url(url: str) -> bool:
    """判断地址是否指向占位/广告流(而非真实视频)。"""
    if not url:
        return False
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return False
    return any(h in path for h in _PLACEHOLDER_HINTS)


def parse_m3u8(text: str, base_url: str) -> Playlist:
    """解析 m3u8 文本, base_url 用于拼接相对地址。"""
    lines = [ln.strip() for ln in text.splitlines()]
    if is_master(text):
        return _parse_master(lines, base_url)
    return _parse_media(lines, base_url)


def _parse_master(lines: List[str], base_url: str) -> MasterPlaylist:
    variants: List[Variant] = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = parse_attrs(line)
        uri = ""
        for j in range(i + 1, len(lines)):
            if lines[j] and not lines[j].startswith("#"):
                uri = lines[j]
                break
        if not uri:
            continue
        try:
            bw = int(float(attrs.get("BANDWIDTH", "0")))
        except (ValueError, TypeError):
            bw = 0
        variants.append(Variant(
            url=urljoin(base_url, uri),
            bandwidth=bw,
            resolution=attrs.get("RESOLUTION", ""),
            codecs=attrs.get("CODECS", ""),
            name=attrs.get("NAME", ""),
        ))
    return MasterPlaylist(variants=variants)


def _parse_media(lines: List[str], base_url: str) -> MediaPlaylist:
    pl = MediaPlaylist()
    key: Optional[KeyInfo] = None
    seg_duration = 0.0
    seg_index = 0
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-KEY"):
            attrs = parse_attrs(line)
            method = attrs.get("METHOD", "NONE")
            if method and method != "NONE":
                uri = attrs.get("URI", "")
                key = KeyInfo(
                    method=method,
                    uri=urljoin(base_url, uri) if uri else "",
                    iv=attrs.get("IV", ""),
                    keyformat=attrs.get("KEYFORMAT", ""),
                )
        elif line.startswith("#EXT-X-MAP"):
            attrs = parse_attrs(line)
            uri = attrs.get("URI", "")
            if uri:
                pl.init_url = urljoin(base_url, uri)
        elif line.startswith("#EXT-X-TARGETDURATION"):
            m = re.match(r"#EXT-X-TARGETDURATION:(\d+)", line)
            if m:
                pl.target_duration = float(m.group(1))
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            m = re.match(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", line)
            if m:
                pl.media_sequence = int(m.group(1))
        elif line.startswith("#EXT-X-ENDLIST"):
            pl.is_live = False
        elif line.startswith("#EXTINF"):
            m = _EXTINF_RE.match(line)
            seg_duration = float(m.group(1)) if m else 0.0
        elif line and not line.startswith("#"):
            pl.segments.append(Segment(
                url=urljoin(base_url, line),
                duration=seg_duration,
                index=seg_index,
            ))
            pl.duration += seg_duration
            seg_index += 1
            seg_duration = 0.0
    pl.key = key
    return pl


def format_duration(seconds: float) -> str:
    """时长格式化: 3661 -> 1h01m01s, 61 -> 1m01s, 44.5 -> 44s"""
    if seconds <= 0:
        return ""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def format_size(num_bytes: float) -> str:
    """字节数格式化: 1536 -> 1.50 KB"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"
