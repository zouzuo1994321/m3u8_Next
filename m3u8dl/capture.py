"""缓存捕捉 —— 逆向自猫抓 cat-catch ``catch-script/catch.js``。

原理（为什么它能拿到别人拿不到的流）
--------------------------------------
站点播放器的数据通路永远是::

    网络分片 -> （可选 AES 解密）-> SourceBuffer.appendBuffer(bytes) -> 解码器

``appendBuffer`` 收到的是**已经解密、已经去包装的裸媒体字节**。所以在
``MediaSource.prototype.addSourceBuffer`` 上做代理、再把每个 SourceBuffer 的
``appendBuffer`` 参数留存下来，就等于绕过了：

- AES-128 加密（不需要 KEY）
- 图片/自定义包装伪装（拿到的是剥离后的数据）
- 分片级防盗链 token（数据已经到位，不必再请求）

代价：需要让播放器把目标区间**缓冲一遍**，且要占一份等量磁盘（这里落临时文件）。

针对本项目的两处关键适配
------------------------
1. **编解码器缺失兜底**：开源版 QtWebEngine 没有 H.264/AAC，站点播放器
   ``addSourceBuffer('video/mp4; codecs="avc1..."')`` 可能直接抛
   ``NotSupportedError``，钩子就挂不上。这里 catch 后用 WebM 的 mimeType 重建
   SourceBuffer —— 播放本来就废了，但 ``appendBuffer`` 会被正常调用，**字节仍能
   完整捕获**。这恰好利用了「捕捉不需要解码」这一点。
2. **先拷贝再 append**：引擎很可能因编解码器不匹配拒绝这块数据，所以必须在调用
   原 ``appendBuffer`` 之前就把字节拷出来，否则一抛异常就丢了。

数据流
------
``appendBuffer`` -> 拷贝 -> base64 -> QWebChannel -> ``CaptureStore.add_chunk``
-> 立即写临时文件（只记偏移量与是否容器头）-> ``save`` 时按偏移切片并丢弃多余头。

内存占用因此与捕获总量无关。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .bridge import B64_ENCODER_JS

# ---------------------------------------------------------------- 容器识别
_FTYP = (b"ftyp", b"styp")   # data[4:8]
_MOOF = b"moof"
_WEBM = b"\x1a\x45\xdf\xa3"  # EBML
_ID3 = b"ID3"
_TS_SYNC = 0x47


def is_mp4_header(chunk: bytes) -> bool:
    """``[size(4)]['f'|'s']['t']['y']['p']`` -> ftyp / styp。"""
    return len(chunk) > 8 and chunk[4:8] in _FTYP


def is_moof(chunk: bytes) -> bool:
    return len(chunk) > 8 and chunk[4:8] == _MOOF


def is_webm_header(chunk: bytes) -> bool:
    return len(chunk) > 4 and chunk[:4] == _WEBM


def is_ts(data: bytes) -> bool:
    """TS 判定: 512 字节内出现 0x47 且间隔 188 处仍是 0x47。"""
    for i in range(min(512, max(0, len(data) - 188))):
        if data[i] == _TS_SYNC and data[i + 188] == _TS_SYNC:
            return True
    return False



def is_header_chunk(chunk: bytes) -> bool:
    """fMP4 的 ftyp/styp 头或 WebM 的 EBML 头（对应原 lastHeaderIndex 判定）。"""
    return is_mp4_header(chunk) or is_webm_header(chunk)


def _ext_for(mime: str, head: bytes) -> str:
    m = (mime or "").lower()
    if "webm" in m:
        return "webm"
    if "mp4" in m or "iso" in m:
        return "mp4"
    if is_webm_header(head):
        return "webm"
    if is_mp4_header(head) or is_moof(head):
        return "mp4"
    if is_ts(head):
        return "ts"
    if m.startswith("audio/"):
        return "mp3"
    return "mp4"


# ---------------------------------------------------------------- 注入脚本
SCRIPT_NAME = "m3u8dl-capture"

_JS = r"""
(function () {
  if (window.__M3U8DL_CAP_READY) { return; }
  window.__M3U8DL_CAP_READY = true;

  var SID = "__SID__";
  var state = window.__M3U8DL_CAP = {
    on: false,        // 是否真正回传（默认只挂钩, 避免常开开销）
    done: false,      // 是否收到 endOfStream
    ready: false,     // 桥是否就绪
    bytes: 0,
    chunks: 0,
    streams: 0,
    errors: 0,
    fallback: 0,      // 因编解码器缺失而替换 mimeType 的次数
    queued: 0
  };

  __B64__

  var queue = [];          // [{mime, index, b64}]
  var pumping = false;

  function pump() {
    var ch = window.__M3U8DL_CH;
    if (!ch) { state.ready = false; return; }
    state.ready = true;
    if (pumping) { return; }
    pumping = true;
    try {
      while (queue.length) {
        var item = queue.shift();
        state.queued = queue.length;
        try { ch.chunk(SID, item.mime, item.index, item.b64); }
        catch (e) { state.errors++; }
      }
    } finally {
      pumping = false;
    }
  }
  // 每 120ms 抽一次队列: 不在 appendBuffer 里同步做 base64 + 跨语言调用,
  // 避免拖慢播放；同时队列不会无限增长。
  setInterval(pump, 120);

  function enqueue(mime, index, buf) {
    try {
      var u8 = new Uint8Array(buf);
      queue.push({ mime: mime, index: index, b64: __m3u8dlB64(u8) });
      state.queued = queue.length;
      // 队列过长时立即抽一次, 防止内存堆积
      if (queue.length >= 8) { pump(); }
    } catch (e) { state.errors++; }
  }

  // 备用 mimeType: 只为让 addSourceBuffer 建得起来, 从而拿到 appendBuffer 钩子
  function fallbackMime(mime) {
    var m = String(mime || "").toLowerCase();
    if (m.indexOf("audio") >= 0) { return 'audio/webm; codecs="vorbis"'; }
    return 'video/webm; codecs="vp9"';
  }

  try {
    var MS = window.MediaSource;
    if (!MS || !MS.prototype) { return; }
    var _add = MS.prototype.addSourceBuffer;
    var _eos = MS.prototype.endOfStream;
    var streamSeq = 0;

    MS.prototype.addSourceBuffer = function (mime) {
      var sb = null;
      var mimeType = String(mime || "");
      try {
        sb = _add.apply(this, arguments);
      } catch (e) {
        try {
          sb = _add.call(this, fallbackMime(mimeType));
          state.fallback++;
        } catch (e2) { throw e; }
      }
      try {
        var index = streamSeq++;
        state.streams = streamSeq;
        var _append = sb.appendBuffer;
        sb.appendBuffer = function (data) {
          // 先拷再 append: 引擎可能拒绝这块数据, 必须先留住字节
          if (state.on && data) {
            try {
              var copy = null;
              if (data instanceof ArrayBuffer) {
                copy = data.slice(0);
              } else if (data && data.buffer instanceof ArrayBuffer) {
                copy = data.buffer.slice(data.byteOffset,
                                         data.byteOffset + data.byteLength);
              }
              if (copy && copy.byteLength > 0) {
                state.bytes += copy.byteLength;
                state.chunks++;
                enqueue(mimeType, index, copy);
              }
            } catch (e) { state.errors++; }
          }
          return _append.apply(this, arguments);
        };
        try { sb.appendBuffer.toString = function () { return _append.toString(); }; } catch (e) {}
      } catch (e) {}
      return sb;
    };
    try { MS.prototype.addSourceBuffer.toString = function () { return _add.toString(); }; } catch (e) {}

    MS.prototype.endOfStream = function () {
      var ret = _eos.apply(this, arguments);
      try {
        state.done = true;
        var ch = window.__M3U8DL_CH;
        if (ch) { try { ch.meta(SID, "done", "1"); } catch (e) {} }
        pump();
      } catch (e) {}
      return ret;
    };
    try { MS.prototype.endOfStream.toString = function () { return _eos.toString(); }; } catch (e) {}
  } catch (e) {}
})();
"""


def build_script(sid: str) -> str:
    """生成注入脚本（sid 区分页面会话）。"""
    safe = "".join(ch for ch in str(sid) if ch.isalnum()) or "0"
    return (_JS.replace("__SID__", safe)
               .replace("__B64__", B64_ENCODER_JS))


ENABLE_JS = "window.__M3U8DL_CAP && (window.__M3U8DL_CAP.on = true), true;"
DISABLE_JS = "window.__M3U8DL_CAP && (window.__M3U8DL_CAP.on = false), true;"

STATS_JS = (
    "(function(){var s=window.__M3U8DL_CAP;"
    "if(!s){return '';}"
    "return JSON.stringify({ready:!!window.__M3U8DL_CH,on:s.on,done:s.done,"
    "bytes:s.bytes,chunks:s.chunks,streams:s.streams,errors:s.errors,"
    "fallback:s.fallback,queued:s.queued});})()"
)

#: 让页面里的 video 回到 0 秒重新缓冲（对应原 resetVideoPlayback）
REWIND_JS = r"""
(function () {
  var n = 0;
  try {
    document.querySelectorAll("video").forEach(function (v) {
      try {
        if (v.currentTime > 0.5) { v.currentTime = 0; }
        if (v.paused) { try { v.play(); } catch (e) {} }
        n++;
      } catch (e) {}
    });
  } catch (e) {}
  return n;
})()
"""


# ---------------------------------------------------------------- 数据存储
@dataclass
class _Stream:
    mime: str
    index: int
    path: Path
    fh: object = None
    size: int = 0
    chunks: int = 0
    #: [(offset, length, is_header)]
    marks: List[Tuple[int, int, bool]] = field(default_factory=list)
    head: bytes = b""


class CaptureStore:
    """边收边写临时文件，内存占用与捕获总量无关。

    线程安全：QWebChannel 槽在 UI 线程触发，保存可能在后台线程。
    """

    def __init__(self, work_dir: Optional[Path] = None):
        self._lock = threading.Lock()
        self._work = Path(work_dir) if work_dir else None
        self._streams: Dict[Tuple[str, int], _Stream] = {}
        self._meta: Dict[str, Dict[str, str]] = {}
        self.total_bytes = 0
        self.total_chunks = 0
        self.last_activity = 0.0
        self._started = time.time()

    # ---- 目录
    def _ensure_work(self) -> Path:
        if self._work is None:
            base = Path(__file__).resolve().parent.parent / ".test_work" / "capture"
            self._work = base
        self._work.mkdir(parents=True, exist_ok=True)
        return self._work

    # ---- 写入
    def add_chunk(self, sid: str, mime: str, index: int, data: bytes) -> None:
        if not data:
            return
        key = (mime or "", int(index))
        with self._lock:
            st = self._streams.get(key)
            if st is None:
                work = self._ensure_work()
                path = work / f"cap_{len(self._streams)}_{int(index)}.part"
                st = _Stream(mime=mime or "", index=int(index), path=path)
                st.fh = open(path, "wb")
                self._streams[key] = st
            offset = st.size
            header = is_header_chunk(data) if offset < 4096 else False
            try:
                st.fh.write(data)
                st.fh.flush()
            except Exception:
                pass
            st.size += len(data)
            st.chunks += 1
            if not st.head:
                st.head = data[:16]
            if header:
                st.marks.append((offset, len(data), True))
            self.total_bytes += len(data)
            self.total_chunks += 1
            self.last_activity = time.time()

    def add_meta(self, sid: str, key: str, value: str) -> None:
        with self._lock:
            self._meta.setdefault(sid or "", {})[key] = value

    def meta(self, sid: str, key: str, default: str = "") -> str:
        with self._lock:
            return self._meta.get(sid or "", {}).get(key, default)

    # ---- 读取
    def streams(self) -> List[Tuple[str, int, int]]:
        """``[(mime, index, bytes)]``，按 index 升序。"""
        with self._lock:
            out = [(st.mime, st.index, st.size)
                   for st in self._streams.values() if st.size]
            out.sort(key=lambda x: x[1])
            return out

    def chunk_count(self) -> int:
        with self._lock:
            return self.total_chunks

    def is_empty(self) -> bool:
        with self._lock:
            return not any(st.size for st in self._streams.values())

    def clear(self) -> None:
        with self._lock:
            for st in self._streams.values():
                try:
                    if st.fh:
                        st.fh.close()
                except Exception:
                    pass
                try:
                    st.path.unlink()
                except Exception:
                    pass
            self._streams.clear()
            self._meta.clear()
            self.total_bytes = 0
            self.total_chunks = 0
            self._started = time.time()

    # ---- 保存
    def save(self, out_dir: Path, base_name: str,
             drop_extra_headers: bool = True,
             on_log=None) -> List[Path]:
        """把每路流写成文件。

        单路 -> ``<base_name>.<ext>``；多路（音视频分离）-> ``<base_name>.<n>.<ext>``。
        ``drop_extra_headers`` 时从最后一个容器头开始写（丢掉重复的 ftyp/EBML）。
        """
        with self._lock:
            snapshot = [st for st in self._streams.values() if st.size]
            for st in snapshot:
                try:
                    if st.fh:
                        st.fh.flush()
                except Exception:
                    pass
        if not snapshot:
            return []

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        snapshot.sort(key=lambda s: s.index)
        multi = len(snapshot) > 1

        written: List[Path] = []
        for n, st in enumerate(snapshot, start=1):
            start = 0
            if drop_extra_headers and st.marks:
                start = st.marks[-1][0]        # 最后一个头部所在偏移
            ext = _ext_for(st.mime, st.head)
            name = f"{base_name}.{n}.{ext}" if multi else f"{base_name}.{ext}"
            target = out_dir / name
            copied = 0
            try:
                with open(st.path, "rb") as src, open(target, "wb") as dst:
                    src.seek(start)
                    while True:
                        buf = src.read(1024 * 1024)
                        if not buf:
                            break
                        dst.write(buf)
                        copied += len(buf)
            except Exception as e:
                if on_log:
                    on_log(f"保存捕获流失败: {e}")
                continue
            if on_log:
                drop = st.size - copied
                tag = f", 丢弃前导 {drop} 字节重复头" if drop > 0 else ""
                on_log(f"已保存捕获流{n}: {target.name} "
                       f"({st.chunks} 块, {copied / 1048576:.2f} MB, "
                       f"mime={st.mime or '未知'}{tag})")
            written.append(target)
        return written


# ---------------------------------------------------------------- ffmpeg 合成
def mux_with_ffmpeg(files: List[Path], out_path: Path,
                    ffmpeg: Optional[str] = None, on_log=None) -> Optional[Path]:
    """把音视频分离的多路文件合成为一个 mp4（``-c copy`` 不重编码）。"""
    from .fftools import run_hidden

    if len(files) < 2:
        return None
    exe = ffmpeg
    if not exe:
        from .fftools import find_ffmpeg
        exe = find_ffmpeg()
    if not exe:
        if on_log:
            on_log("未检测到 ffmpeg, 跳过音视频合成(已保留分离文件)")
        return None

    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error"]
    for f in files:
        cmd += ["-i", str(f)]
    cmd += ["-c", "copy", "-map", "0", "-map", "1", str(out_path)]
    try:
        r = run_hidden(cmd, capture_output=True, timeout=1800)
        if r.returncode == 0 and out_path.exists():
            if on_log:
                on_log(f"音视频已合成: {out_path.name}")
            return out_path
        if on_log:
            err = (r.stderr or b"").decode("utf-8", "ignore")[:400]
            on_log(f"音视频合成失败: {err.strip()}")
    except Exception as e:
        if on_log:
            on_log(f"音视频合成异常: {e}")
    return None
