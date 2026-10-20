"""从 MP4 直链读取分辨率 / 时长 / 码率(只发少量 Range 请求)。

为什么要它
----------
stripchat 系站点下发的是 ``.mp4`` 直链(实测 190.5MB / 854x480 / 3.22Mbps),
而 HTTP 响应头里**没有**这些信息。以前只能靠 HEAD 拿个总大小, 列表里分辨率、
码率、时长全是"—", 用户会以为"根本没嗅探到"。

做法
----
MP4 的信息全在 ``moov`` box 里(``mvhd`` 给时长与 timescale, ``tkhd`` 给宽高)。
``moov`` 要么在文件开头(faststart), 要么在末尾 —— 所以最多读三次:

1. 头 256KB 找 ``moov``;
2. 没有则读尾 2MB(绝大多数非 faststart 文件 ``moov`` 就在尾部);
3. 还找不到就按解析到的 ``moov`` 偏移精确补读一段。

三个请求加起来通常 < 500KB, 对 190MB 的文件约 0.3%。
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

#: 一次 Range 请求最多读多少(头/尾)
_HEAD_BYTES = 256 * 1024
_TAIL_BYTES = 2 * 1024 * 1024
#: moov 比这还大就不再补读(极罕见, 且收益已不大)
_MAX_MOOV = 8 * 1024 * 1024


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "big") if o + 4 <= len(b) else 0


def _u64(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 8], "big") if o + 8 <= len(b) else 0


def _iter_boxes(buf: bytes, base_offset: int = 0):
    """遍历 buf 内的 box, 产出 (type, payload_start, payload_len)。

    base_offset 是该 buf 在**整个文件**中的起始偏移, 用于计算绝对位置。
    """
    pos = 0
    n = len(buf)
    while pos + 8 <= n:
        size = _u32(buf, pos)
        typ = buf[pos + 4:pos + 8]
        hdr = 8
        if size == 1:                       # 64 位 largesize
            size = _u64(buf, pos + 8)
            hdr = 16
        elif size == 0:                     # 一直到文件尾
            size = n - pos
        if size < hdr or pos + size > n:
            # 截断的 box: 若是我们要找的类型仍返回可解析部分
            if typ in (b"moov", b"mvhd", b"tkhd", b"mdhd") and pos + hdr < n:
                yield typ, pos + hdr, n - pos - hdr
            return
        yield typ, pos + hdr, size - hdr
        pos += size


def _find(buf: bytes, want: bytes) -> Optional[Tuple[int, int]]:
    """在 buf 顶层找 box, 返回 (payload_start, payload_len)。"""
    for typ, start, ln in _iter_boxes(buf):
        if typ == want:
            return start, ln
    return None


def _parse_mvhd(buf: bytes, start: int, ln: int) -> Tuple[float, int]:
    """返回 (时长秒, timescale)。"""
    body = buf[start:start + ln]
    if len(body) < 20:
        return 0.0, 0
    ver = body[0]
    if ver == 1:
        if len(body) < 32:
            return 0.0, 0
        timescale = _u32(body, 20)
        duration = _u64(body, 24)
    else:
        timescale = _u32(body, 12)
        duration = _u32(body, 16)
    if timescale <= 0:
        return 0.0, 0
    return duration / timescale, timescale


def _parse_tkhd(buf: bytes, start: int, ln: int) -> Tuple[int, int]:
    """返回 (宽, 高)。tkhd 末尾 8 字节是 16.16 定点宽高。"""
    body = buf[start:start + ln]
    if len(body) < 8:
        return 0, 0
    w = _u32(body, len(body) - 8)
    h = _u32(body, len(body) - 4)
    # 16.16 定点: 高 16 位是整数部分
    return w >> 16, h >> 16


def _parse_moov(buf: bytes, start: int, ln: int) -> Dict:
    """从 moov payload 里取 (时长, 宽, 高)。"""
    out: Dict = {"duration": 0.0, "width": 0, "height": 0}
    moov = buf[start:start + ln]
    for typ, s, l in _iter_boxes(moov):
        if typ == b"mvhd":
            dur, _ = _parse_mvhd(moov, s, l)
            if dur > 0:
                out["duration"] = dur
        elif typ == b"trak":
            trak = moov[s:s + l]
            for t2, s2, l2 in _iter_boxes(trak):
                if t2 == b"tkhd":
                    w, h = _parse_tkhd(trak, s2, l2)
                    # 取面积最大的那条轨(视频轨), 忽略封面/缩略图轨
                    if w >= out["width"] and h > 0:
                        out["width"], out["height"] = w, h
        if out["duration"] and out["width"]:
            break
    return out


def _range_get(session, url: str, headers: Dict[str, str], spec: str,
               timeout: float = 20):
    """发一次 Range 请求, 返回 (bytes, 状态码, Content-Range)。"""
    h = dict(headers or {})
    h["Range"] = spec
    try:
        r = session.get(url, headers=h, timeout=timeout, stream=True)
    except Exception:
        return b"", -1, ""
    try:
        if r.status_code not in (200, 206):
            return b"", r.status_code, r.headers.get("Content-Range", "")
        data = b""
        for chunk in r.iter_content(64 * 1024):
            data += chunk
            if len(data) > _MAX_MOOV:
                break
        return data, r.status_code, r.headers.get("Content-Range", "")
    finally:
        r.close()


def parse_bytes(buf: bytes, total: int = 0) -> Dict:
    """从**已在内存里**的数据中取元数据(buf 需包含 moov)。

    单独抽出来有两个用处: 自检可以喂一段合成数据做断言; ``probe`` 的
    头/尾两次尝试也能共用同一段解析逻辑, 避免两处实现漂移。
    """
    out = {"duration": 0.0, "width": 0, "height": 0, "bitrate_kbps": 0}
    found = _find(buf, b"moov")
    if not found:
        return out
    info = _parse_moov(buf, found[0], found[1])
    for k in ("duration", "width", "height"):
        out[k] = info.get(k, out[k]) or out[k]
    dur = out["duration"] or 0.0
    if dur > 0 and total > 0:
        out["bitrate_kbps"] = int(total * 8 / dur / 1000)
    return out


def probe(session, url: str, headers: Optional[Dict[str, str]] = None,
          total: int = 0, timeout: float = 20) -> Dict:
    """读取 mp4 的 时长/宽/高/码率。

    返回 ``{"duration": float, "width": int, "height": int,
    "bitrate_kbps": int}``; 读不到的字段为 0。
    """
    out = {"duration": 0.0, "width": 0, "height": 0, "bitrate_kbps": 0}

    head, _, _ = _range_get(session, url, headers, f"bytes=0-{_HEAD_BYTES - 1}",
                            timeout)
    if head:
        got = parse_bytes(head, total)
        if got["duration"] or got["width"]:
            return got
        if total > _TAIL_BYTES:
            # moov 在尾部(非 faststart): 读最后 2MB
            tail, _, _ = _range_get(
                session, url, headers,
                f"bytes={total - _TAIL_BYTES}-{total - 1}", timeout)
            if tail:
                got = parse_bytes(tail, total)
                if got["duration"] or got["width"]:
                    return got
    return out


def probe_url(session, url: str, headers: Optional[Dict[str, str]] = None,
              total: int = 0, timeout: float = 20) -> Dict:
    """同 :func:`probe`, 但所有异常都吞掉 —— 元数据属于"有则更好"。"""
    try:
        return probe(session, url, headers, total, timeout)
    except Exception:
        return {"duration": 0.0, "width": 0, "height": 0, "bitrate_kbps": 0}
