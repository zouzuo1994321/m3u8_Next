"""更多功能 —— 逆向自猫抓 ``js/m3u8.js`` 的「更多功能」面板与下载管线。

对应关系（原实现的按钮 -> 这里的函数）
---------------------------------------
===========================  ==================================================
猫抓                          本模块
===========================  ==================================================
``#downText`` 下载分片列表     :func:`export_fragment_list`
``#tsAddArg`` 获取/追加参数     :func:`append_query_params`
``#regular`` 正则筛选           :func:`select_by_regex`
``#invertSelection`` 反选      :func:`invert_selection`
``#dataPreprocessing`` 预处理   :func:`strip_image_wrapper`
``#searchingForRealKey`` 找真KEY :func:`brute_force_key`
``#onlyAudio`` 仅音频           ffmpeg 参数 ``-vn``（见 :func:`audio_only_args`）
``#skipDecrypt`` 跳过解密       ``DownloadConfig.skip_decrypt``
===========================  ==================================================

其中两个技巧值得单独说明：

**图片伪装剥离**：部分站点把 TS 分片前面拼一张合法 PNG/JPG 头，靠
``Content-Type: image/png`` 骗过 CDN 与自动化检测。分片本体其实是图片末尾
到文件末尾那段。原实现在下载管线里插了一个 ``use()`` 中间件做字节级切除，
这里实现成纯函数，便于单测。

**密钥爆破验证**：深度搜索会捞到一堆「疑似 16 字节 KEY」（数组、hex、base64 都
可能）。逐个拿它去 AES-128-CBC 解密**第一个分片**，再看解密结果是否以合法容器
签名开头——签名对上了，这个 KEY 就是真的。这比人眼逐个试快得多。
"""
from __future__ import annotations

import binascii
import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .capture import is_ts, is_webm_header

# ---------------------------------------------------------------- 图片伪装剥离
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_JPG_SIG = b"\xff\xd8\xff"
_PNG_IEND = b"IEND"
_JPG_EOI = b"\xff\xd9"


def strip_image_wrapper(data: bytes) -> bytes:
    """剥掉分片前面伪装的 PNG / JPG 头。

    找不到头或找不到结束标记时**原样返回**（宁可不动，也不能破坏数据）。
    """
    n = len(data)
    if n < 8:
        return data

    if data.startswith(_PNG_SIG[:4]):
        # PNG: 找到 IEND 后跟 4 字节 CRC，跳过后即为载荷
        pos = data.find(_PNG_IEND)
        if pos != -1:
            start = pos + 8
            if 0 < start < n:
                return data[start:]
        return data

    if data.startswith(_JPG_SIG):
        pos = data.find(_JPG_EOI)
        if pos != -1:
            start = pos + 2
            if 0 < start < n:
                return data[start:]
        return data

    return data


def detect_wrapper(data: bytes) -> str:
    """返回伪装类型: ``png`` / ``jpeg`` / ``""``。"""
    if data.startswith(_PNG_SIG[:4]):
        return "png"
    if data.startswith(_JPG_SIG):
        return "jpeg"
    return ""


# ---------------------------------------------------------------- 分片参数追加
def append_query_params(url: str, extra: str) -> str:
    """给分片地址追加查询参数（``tsAddArg``）。

    ``extra`` 形如 ``token=abc&exp=123``，会与原有 query 合并（同名覆盖）。
    """
    extra = (extra or "").strip().lstrip("?")
    if not extra:
        return url
    try:
        parts = urlsplit(url)
        q = dict(parse_qsl(parts.query, keep_blank_values=True))
        for k, v in parse_qsl(extra, keep_blank_values=True):
            q[k] = v
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(q), parts.fragment))
    except Exception:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{extra}"


# ---------------------------------------------------------------- 分片筛选
def select_by_regex(urls: Sequence[str], pattern: str) -> List[bool]:
    """返回每个分片是否命中正则（对应原 ``#regular`` 回车筛选）。"""
    if not pattern:
        return [True] * len(urls)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"正则表达式错误: {e}") from e
    return [bool(rx.search(u)) for u in urls]


def invert_selection(flags: Sequence[bool]) -> List[bool]:
    """反选。"""
    return [not f for f in flags]


def selected_indices(flags: Sequence[bool]) -> List[int]:
    return [i for i, f in enumerate(flags) if f]


def export_fragment_list(segments: Sequence, out_path: Path,
                         indices: Optional[Iterable[int]] = None) -> Tuple[Path, int]:
    """把分片地址逐行写成 txt（对应原 ``#downText``），可用于外部工具续传。"""
    idx = list(indices) if indices is not None else list(range(len(segments)))
    lines = []
    for i in idx:
        if 0 <= i < len(segments):
            lines.append(segments[i].url)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out_path, len(lines)


# ---------------------------------------------------------------- 密钥爆破
def _decrypt_cbc(data: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(data) % 16:
        data = data[: len(data) - (len(data) % 16)]
    if not data:
        return b""
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def looks_like_decrypted(data: bytes) -> bool:
    """解密结果是否是合法容器开头（对应原 ``check()`` 的签名表）。"""
    if not data:
        return False
    if len(data) > 8 and data[4:8] in (b"ftyp", b"styp", b"moof"):
        return True
    if is_webm_header(data):
        return True
    if data.startswith(b"ID3"):
        return True
    if len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return True                      # MPEG 音频帧
    if len(data) > 1 and data[0] == 0xFF and (data[1] & 0xF0) == 0xF0:
        return True                      # AAC ADTS
    if is_ts(data):
        return True
    return False


def brute_force_key(sample: bytes, candidates: Sequence[bytes],
                    iv: bytes,
                    strip_wrapper: bool = True) -> Optional[bytes]:
    """用第一个分片的密文逐个试 KEY，返回能解出合法容器的那个。

    ``sample`` 传密文（未解密的分片原始字节）；``iv`` 按 HLS 规范通常为分片序号
    大端 16 字节。
    """
    if not sample or not candidates:
        return None
    if len(iv) != 16:
        raise ValueError("IV 必须是 16 字节")
    for key in candidates:
        if len(key) != 16:
            continue
        try:
            plain = _decrypt_cbc(sample, key, iv)
        except Exception:
            continue
        if strip_wrapper:
            plain = strip_image_wrapper(plain)
        if looks_like_decrypted(plain):
            return key
    return None


def key_to_hex(key: bytes) -> str:
    return binascii.hexlify(key).decode()


def key_to_b64(key: bytes) -> str:
    import base64

    return base64.b64encode(key).decode()


def audio_only_args(ffmpeg_args: Optional[List[str]] = None) -> List[str]:
    """仅音频: 丢弃视频轨（对应原 ``#onlyAudio``）。"""
    args = list(ffmpeg_args or [])
    if "-vn" not in args:
        args.append("-vn")
    return args


# ---------------------------------------------------------------- 伪装探测提示
def describe_wrapper(data: bytes) -> str:
    kind = detect_wrapper(data)
    if not kind:
        return ""
    stripped = strip_image_wrapper(data)
    return (f"检测到 {kind.upper()} 伪装头: {len(data)} -> "
            f"{len(stripped)} 字节（剥离 {len(data) - len(stripped)}）")
