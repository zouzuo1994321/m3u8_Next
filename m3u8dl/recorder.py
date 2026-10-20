"""录制与自动化 —— 逆向自猫抓 cat-catch 的 6 个按钮。

对应关系（原实现 -> 本模块）
---------------------------
===============================  ==================  ==========================
猫抓                             原文件              实现要点
===============================  ==================  ==========================
``视频录制``                     ``recorder.js``     `<video>/<audio>.captureStream(fps)`
                                                     -> ``MediaRecorder`` -> webm
``录制webRTC``                   ``webrtc.js``       Proxy ``RTCPeerConnection``, 在
                                                     ``track`` 事件里收 MediaStreamTrack,
                                                     攒够音视频后录
``屏幕捕捉``                     ``recorder2.js``    ``getDisplayMedia`` -> 录屏
``模拟手机``                     ``background.js``   切 UA 到 iPhone 后重载
``自动下载``                     ``catch.js``        捕获完成后自动入队
``数据发送``                     ``popup.js``        POST 捕获结果到外部服务
===============================  ==================  ==========================

为什么录出来的格式是 webm
--------------------------
``MediaRecorder`` 只能输出浏览器支持的编码。开源版 QtWebEngine **不含**
H.264/AAC, 但含 VP8/VP9/Opus —— 所以 ``video/webm;codecs=vp9,opus`` 可用,
``video/mp4`` 不可用。这也正好解释了本项目的定位: 页面内 H.264 视频**播不了**,
但 webRTC 的 VP8/VP9 流能播能录; 而 H.264 直播则应该走「缓存捕捉」
(拿已解码前的裸字节) 或直接下载 m3u8。

媒体元素录制的局限
------------------
``captureStream`` 只能录**已经在解码的**内容。页面里的 H.264 视频在本引擎中
无法解码, 因此录不到画面(会得到空数据)。这个能力适合: 页面用 VP8/VP9 播放的
场景、以及 webRTC。这点在原版猫抓里不存在(Chrome 有全部解码器), 属于移植适配。
"""
from __future__ import annotations

import re
import shutil
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from .bridge import B64_ENCODER_JS

SCRIPT_NAME = "m3u8dl-recorder"

#: 推荐给页面的录制参数
DEFAULT_VIDEO_BITS = 4_000_000
DEFAULT_AUDIO_BITS = 128_000
DEFAULT_FPS = 0          # 0 = 跟随源帧率

#: iOS Safari 的 UA（与猫抓 MobileUserAgent 同源思路）
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1"
)

_JS = r"""
(function () {
  if (window.__M3U8DL_REC_READY) { return; }
  window.__M3U8DL_REC_READY = true;

  var SID = "__SID__";
  __B64__

  var R = window.__M3U8DL_REC = {
    rec: null, pending: [], pumping: false,
    chunks: 0, bytes: 0, mime: "", kind: "", state: "idle",
    startedAt: 0, error: "", tracks: { video: [], audio: [] }
  };

  // ---- 把录到的分片回传 Python ----
  function pump() {
    var ch = window.__M3U8DL_CH;
    if (!ch || typeof ch.rec !== "function" || R.pumping) { return; }
    R.pumping = true;
    try {
      while (R.pending.length) {
        var b64 = R.pending.shift();
        try { ch.rec(SID, R.kind, b64); } catch (e) {}
      }
    } finally { R.pumping = false; }
  }
  setInterval(pump, 120);

  // ---- 编码协商(原实现遍历 isTypeSupported) ----
  var CANDIDATES = [
    "video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus",
    "video/webm;codecs=vp9", "video/webm;codecs=vp8",
    "video/webm", "audio/webm;codecs=opus", "audio/webm"
  ];
  function supportedMimes() {
    var out = [];
    try {
      for (var i = 0; i < CANDIDATES.length; i++) {
        if (MediaRecorder.isTypeSupported(CANDIDATES[i])) { out.push(CANDIDATES[i]); }
      }
    } catch (e) {}
    return out;
  }
  window.__M3U8DL_REC_MIMES = supportedMimes;

  function pickMime(prefer) {
    try {
      if (prefer && MediaRecorder.isTypeSupported(prefer)) { return prefer; }
    } catch (e) {}
    var list = supportedMimes();
    return list.length ? list[0] : "";
  }

  // ---- 可录制的媒体元素(给 UI 选) ----
  window.__M3U8DL_REC_SOURCES = function () {
    var out = [];
    try {
      var els = document.querySelectorAll("video,audio");
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        out.push({
          i: i,
          kind: el.tagName.toLowerCase(),
          label: String(el.currentSrc || el.src || "(内存流)").slice(0, 90),
          playing: !el.paused && !el.ended,
          w: el.videoWidth || 0,
          h: el.videoHeight || 0
        });
      }
    } catch (e) {}
    return out;
  };

  window.__M3U8DL_REC_STATS = function () {
    return {
      state: R.state, mime: R.mime, kind: R.kind,
      chunks: R.chunks, bytes: R.bytes, error: R.error,
      queued: R.pending.length,
      secs: R.startedAt ? Math.round((Date.now() - R.startedAt) / 1000) : 0,
      webrtc: R.tracks.video.length + R.tracks.audio.length
    };
  };

  function begin(stream, opt) {
    var mime = pickMime(opt.mime);
    if (!mime) {
      R.error = "当前环境没有可用的录制编码(需 VP8/VP9/Opus)";
      R.state = "error";
      return "no-mime";
    }
    var ropt = { mimeType: mime };
    if (opt.videoBits) { ropt.videoBitsPerSecond = opt.videoBits | 0; }
    if (opt.audioBits) { ropt.audioBitsPerSecond = opt.audioBits | 0; }
    var rec;
    try { rec = new MediaRecorder(stream, ropt); }
    catch (e) { R.error = String((e && e.message) || e); R.state = "error"; return "recorder-fail"; }

    R.rec = rec; R.mime = mime; R.kind = opt.kind || "element";
    R.chunks = 0; R.bytes = 0; R.pending = [];
    R.startedAt = Date.now(); R.state = "recording"; R.error = "";

    rec.ondataavailable = function (ev) {
      if (!ev || !ev.data || !ev.data.size) { return; }
      R.bytes += ev.data.size; R.chunks++;
      // 用 FileReader 转 base64: 手写编码器也行, 但 Blob 只能这样读
      var fr = new FileReader();
      fr.onload = function () {
        var s = String(fr.result || "");
        var i = s.indexOf(",");
        if (i > 0) { R.pending.push(s.slice(i + 1)); }
        if (R.pending.length >= 4) { pump(); }
      };
      try { fr.readAsDataURL(ev.data); } catch (e) {}
    };
    rec.onstop = function () {
      R.state = "stopped";
      R.rec = null;
      try { stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
    };
    rec.onerror = function (ev) {
      R.error = String((ev && ev.error && ev.error.name) || "recorder error");
      R.state = "error";
    };
    try {
      // 每秒一片: 内存占用与时长无关, 也能边录边落盘
      rec.start(1000);
    } catch (e) {
      R.error = String((e && e.message) || e); R.state = "error"; return "start-fail";
    }
    return "started";
  }

  window.__M3U8DL_REC_START = function (opt) {
    opt = opt || {};
    if (R.rec) { return "already"; }
    var kind = opt.kind || "element";
    try {
      if (kind === "element") {
        var els = document.querySelectorAll("video,audio");
        if (!els.length) { R.error = "页面内没有 video/audio 元素"; R.state = "error"; return "no-element"; }
        var el = els[opt.index | 0] || els[0];
        var cs = el.captureStream || el.mozCaptureStream;
        if (typeof cs !== "function") {
          R.error = "该元素不支持 captureStream"; R.state = "error"; return "no-capture";
        }
        return begin(opt.fps ? cs.call(el, opt.fps | 0) : cs.call(el), opt);
      }
      if (kind === "webrtc") {
        var v = R.tracks.video[0], a = R.tracks.audio[0], tracks = [];
        if (v) { tracks.push(v); }
        if (a) { tracks.push(a); }
        if (!tracks.length) {
          R.error = "尚未捕获到 webRTC 轨道(需要页面先建立连接)";
          R.state = "error";
          return "no-track";
        }
        return begin(new MediaStream(tracks), opt);
      }
      if (kind === "screen") {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
          R.error = "当前环境不支持 getDisplayMedia"; R.state = "error"; return "no-display";
        }
        R.state = "pending"; R.error = "";
        navigator.mediaDevices.getDisplayMedia({ video: { cursor: "never" }, audio: false })
          .then(function (s) { begin(s, opt); })
          .catch(function (e) {
            R.error = String((e && e.message) || e);
            R.state = "error";
          });
        return "pending";
      }
      R.error = "未知的录制类型: " + kind;
      return "bad-kind";
    } catch (e) {
      R.error = String((e && e.message) || e); R.state = "error";
      return "error";
    }
  };

  window.__M3U8DL_REC_STOP = function () {
    try { if (R.rec && R.rec.state !== "inactive") { R.rec.stop(); } }
    catch (e) {}
    pump();
    return R.state;
  };

  // ---- RTCPeerConnection 代理(原 webrtc.js): 收集远端轨道 ----
  try {
    var _PC = window.RTCPeerConnection;
    if (typeof _PC === "function") {
      var _fnToString = Function.prototype.toString;
      var proxy = new Proxy(_PC, {
        construct: function (target, args) {
          var pc = Reflect.construct(target, args);
          try {
            pc.addEventListener("track", function (ev) {
              var t = ev && ev.track;
              if (!t || (t.kind !== "video" && t.kind !== "audio")) { return; }
              var arr = R.tracks[t.kind];
              arr.push(t);
              if (arr.length > 4) { arr.shift(); }   // 只留最近几条, 防泄漏
            });
          } catch (e) {}
          return pc;
        }
      });
      try {
        Object.defineProperty(proxy, "toString", {
          value: function () { return _fnToString.call(_PC); },
          configurable: true, writable: true
        });
      } catch (e) {}
      window.RTCPeerConnection = proxy;
    }
  } catch (e) {}
})();
"""


def build_script(sid: str) -> str:
    """生成注入脚本。"""
    safe = "".join(ch for ch in str(sid) if ch.isalnum()) or "0"
    return _JS.replace("__SID__", safe).replace("__B64__", B64_ENCODER_JS)


def start_js(kind: str, index: int = 0, mime: str = "",
             video_bits: int = DEFAULT_VIDEO_BITS,
             audio_bits: int = DEFAULT_AUDIO_BITS,
             fps: int = DEFAULT_FPS) -> str:
    import json

    opt = {"kind": kind, "index": int(index), "mime": mime or "",
           "videoBits": int(video_bits), "audioBits": int(audio_bits),
           "fps": int(fps)}
    return f"window.__M3U8DL_REC_START({json.dumps(opt)})"


#: 启动结果 -> 中文提示
START_HINTS = {
    "started": "已开始录制",
    "pending": "等待屏幕授权…",
    "already": "已经在录制中",
    "no-element": "页面内没有可录制的 video/audio 元素",
    "no-capture": "该媒体元素不支持 captureStream",
    "no-track": "尚未捕获到 webRTC 轨道(需页面先建立连接)",
    "no-display": "当前环境不支持屏幕捕捉(getDisplayMedia)",
    "no-mime": "当前环境没有可用的录制编码(需 VP8/VP9/Opus)",
    "recorder-fail": "无法创建 MediaRecorder",
    "start-fail": "MediaRecorder 启动失败",
    "bad-kind": "未知的录制类型",
    "error": "录制启动异常",
}


STOP_JS = "window.__M3U8DL_REC_STOP()"
STATS_JS = ("(function(){var f=window.__M3U8DL_REC_STATS;"
            "return f?JSON.stringify(f()):'';})()")
MIMES_JS = ("(function(){var f=window.__M3U8DL_REC_MIMES;"
            "return f?JSON.stringify(f()):'[]';})()")
#: 供 UI 展示可选媒体元素
SOURCES_JS = ("(function(){var f=window.__M3U8DL_REC_SOURCES;"
              "return f?JSON.stringify(f()):'[]';})()")


def parse_stats(raw) -> dict:
    import json

    if not isinstance(raw, str) or not raw:
        return {}
    try:
        d = json.loads(raw)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def parse_mimes(raw) -> List[str]:
    import json

    try:
        v = json.loads(raw or "[]")
    except Exception:
        return []
    return [x for x in v if isinstance(x, str)]


def parse_sources(raw) -> List[dict]:
    import json

    try:
        v = json.loads(raw or "[]")
    except Exception:
        return []
    return [x for x in v if isinstance(x, dict)]


def ext_for_mime(mime: str) -> str:
    m = (mime or "").lower()
    if "mp4" in m:
        return "mp4"
    if "ogg" in m:
        return "ogv"
    return "webm"


# ---------------------------------------------------------------- 落盘
class RecordStore:
    """把页面回传的录制分片按顺序追加到一个临时文件。

    与 ``capture.CaptureStore`` 的区别: 录制是**单条连续流**, 不需要头部去重,
    也不能丢前导字节(WebM 的 EBML 头只出现在最前面), 所以直接 append。
    """

    def __init__(self, work_dir: Optional[Path] = None):
        self._lock = threading.Lock()
        self._work = Path(work_dir) if work_dir else None
        self._fh = None
        self._path: Optional[Path] = None
        self.kind = ""
        self.bytes = 0
        self.chunks = 0
        self.started_at = 0.0

    def _ensure_work(self) -> Path:
        if self._work is None:
            self._work = Path(__file__).resolve().parent.parent / ".test_work" / "record"
        self._work.mkdir(parents=True, exist_ok=True)
        return self._work

    def begin(self, kind: str = "") -> None:
        """开一个新的录制文件（会丢弃未保存的上一段）。"""
        self.abort()
        with self._lock:
            work = self._ensure_work()
            self._path = work / f"rec_{int(time.time() * 1000) % 10 ** 9}.bin"
            self._fh = open(self._path, "wb")
            self.kind = kind
            self.bytes = 0
            self.chunks = 0
            self.started_at = time.time()

    def add(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._fh is None:
                # 页面可能比 begin 更早推数据(屏幕授权场景), 自动补一个文件
                work = self._ensure_work()
                self._path = work / f"rec_{int(time.time() * 1000) % 10 ** 9}.bin"
                self._fh = open(self._path, "wb")
                self.started_at = time.time()
            try:
                self._fh.write(data)
                self._fh.flush()
            except Exception:
                return
            self.bytes += len(data)
            self.chunks += 1

    @property
    def active(self) -> bool:
        return self._fh is not None

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    def finish(self, out_dir: Path, base_name: str,
               mime: str = "", on_log=None) -> Optional[Path]:
        """把临时文件落成正式文件，返回路径。"""
        with self._lock:
            fh, path = self._fh, self._path
            self._fh = None
            self._path = None
            size = self.bytes
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        if path is None or size <= 0:
            try:
                if path and path.exists():
                    path.unlink()
            except Exception:
                pass
            return None

        ext = ext_for_mime(mime)
        out_dir = Path(out_dir)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / f"{base_name}.{ext}"
            n = 2
            while target.exists():
                target = out_dir / f"{base_name}_{n}.{ext}"
                n += 1
            try:
                path.replace(target)
            except OSError:
                # 临时目录与输出目录常常不在同一个盘（TEMP 在 C:、下载目录在 D:/E:），
                # os.replace 跨盘会抛 WinError 17。退化成复制+删除。
                shutil.move(str(path), str(target))
        except Exception as e:
            if on_log:
                on_log(f"录制文件保存失败: {e}")
            return None
        if on_log:
            on_log(f"录制已保存: {target.name} "
                   f"({size / 1048576:.2f} MB, mime={mime or '未知'})")
        return target

    def abort(self) -> None:
        """丢弃当前录制（不落盘）。"""
        with self._lock:
            fh, path = self._fh, self._path
            self._fh = None
            self._path = None
            self.bytes = 0
            self.chunks = 0
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        if path is not None:
            try:
                path.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------- 数据发送
def build_payload(title: str, page_url: str, videos: List[dict],
                  source: str = "m3u8dl") -> dict:
    """组装「数据发送」的 JSON（对应猫抓 send2local 的载荷）。"""
    return {
        "source": source,
        "title": title or "",
        "page": page_url or "",
        "ts": int(time.time()),
        "count": len(videos or []),
        "videos": list(videos or []),
    }


def send_json(url: str, payload: dict, timeout: int = 10,
              headers: Optional[dict] = None) -> Tuple[bool, str]:
    """POST JSON 到外部服务，返回 ``(成功, 说明)``。"""
    import json

    import requests

    if not url or not re.match(r"^https?://", url.strip(), re.I):
        return False, "请填写以 http:// 或 https:// 开头的地址"
    h = {"Content-Type": "application/json; charset=utf-8",
         "User-Agent": "M3U8DL-DataSender/1.0"}
    h.update(headers or {})
    try:
        r = requests.post(url.strip(), data=json.dumps(payload, ensure_ascii=False)
                          .encode("utf-8"), headers=h, timeout=timeout)
        if 200 <= r.status_code < 300:
            return True, f"已发送 {payload.get('count', 0)} 条 (HTTP {r.status_code})"
        return False, f"服务端返回 HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:
        return False, f"发送失败: {e}"
