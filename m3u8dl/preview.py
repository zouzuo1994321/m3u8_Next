"""内置实时预览: 本地 HTTP 代理 + hls.js 播放器。

设计:
- 启动 127.0.0.1 随机端口的本地 HTTP 服务
- /player?u=<m3u8地址>&h=<请求头JSON> 返回内嵌 hls.js 的播放页面
- /p?u=<目标地址>&h=<请求头JSON> 代理抓取远端资源:
  * m3u8 播放列表: 重写其中的相对/绝对地址为代理地址(解决跨域)
  * ts/mp4/key 分片: 原样转发并附加 CORS 头
- QWebEngineView 加载本地播放页, 实现不依赖外部浏览器的实时预览
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_URI_ATTR_RE = re.compile(r'(URI=)("([^"]*)")')

#: 概率性 TLS 断连(部分 CDN 会随机掐连接) —— 原地重试即可
_TRANSIENT = ("SSLError", "SSLEOFError", "ConnectionError", "Timeout",
              "ChunkedEncodingError", "ProtocolError")


def _get_retry(url: str, headers: dict, attempts: int = 3):
    """GET 并失败重试(仅针对瞬断类错误, 4xx/5xx 不重试)。

    FFmpeg 拉分片是**逐片**请求, 单片失败会直接让播放花屏/中断; 这类
    CDN 实测每次请求有约 15% 概率抛 ``SSL: UNEXPECTED_EOF_WHILE_READING``,
    不重试的话预览基本没法看。
    """
    last = None
    for i in range(max(1, attempts)):
        try:
            return requests.get(url, headers=headers, timeout=(5, 20),
                                stream=True)
        except Exception as e:                # noqa: BLE001
            last = e
            name = type(e).__name__
            if not (name in _TRANSIENT or "SSL" in name) or i >= attempts - 1:
                raise
            time.sleep(0.25 * (i + 1))
    raise last  # pragma: no cover


def _url_basename(url: str, default: str = "playlist.m3u8") -> str:
    """取 URL 路径末段作为代理路径名。

    FFmpeg 的 HLS 解复用器会按分片地址的扩展名判断类型
    (allowed_segment_extensions), 因此代理地址必须保留原始文件名,
    例如 /p/s000.ts?u=... —— 否则会报 "not in allowed_segment_extensions"。
    """
    try:
        name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    except Exception:
        name = ""
    if not name or "." not in name:
        return default
    return name

_PLAYER_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>预览</title>
<style>
html,body{margin:0;padding:0;background:#0d0f13;height:100%;overflow:hidden}
#box{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}
video{max-width:100%;max-height:100%;width:100%;height:100%;background:#000;outline:none}
#st{position:absolute;left:10px;bottom:8px;color:#9aa0a6;font:12px/1.5 sans-serif;
    background:rgba(0,0,0,.45);padding:2px 8px;border-radius:4px}
</style></head><body>
<div id="box"><video id="v" controls autoplay muted playsinline></video></div>
<div id="st">加载中…</div>
<script>__HLS_JS__</script>
<script>
(function(){
  var v=document.getElementById('v'), st=document.getElementById('st');
  var q=new URLSearchParams(location.search);
  var u=q.get('u')||'';
  var h=q.get('h')||'{}';
  if(!u){st.textContent='未指定播放地址';return;}
  var src='/p?u='+encodeURIComponent(u)+'&h='+encodeURIComponent(h);
  function fail(msg){st.textContent='播放失败: '+msg;}
  if(window.Hls&&Hls.isSupported()){
    var hls=new Hls({enableWorker:true,lowLatencyMode:true,backBufferLength:30});
    hls.loadSource(src);hls.attachMedia(v);
    hls.on(Hls.Events.MANIFEST_PARSED,function(){st.textContent='';v.play().catch(function(){});});
    hls.on(Hls.Events.ERROR,function(e,d){
      st.textContent='错误: '+d.details;
      if(d.fatal){try{hls.destroy();}catch(_){}}
    });
  }else if(v.canPlayType('application/vnd.apple.mpegurl')){
    v.src=src;
  }else{fail('当前环境不支持 HLS 播放');}
})();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "M3U8DLPreview/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音访问日志
        pass

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/player":
                self._send_html()
            elif parsed.path.startswith("/mem/"):
                self._serve_memory(parsed)
            elif parsed.path == "/p" or parsed.path.startswith("/p/"):
                self._proxy(qs)
            else:
                self.send_error(404)
        except Exception as e:
            try:
                self.send_error(500, str(e))
            except Exception:
                pass

    def _serve_memory(self, parsed):
        """回放在内存里捕获的 m3u8(深度搜索产物)。

        为什么不能直接喂本地文件: QtMultimedia 的 FFmpeg 后端在输入是**本地
        文件**时, HLS 解复用器的协议白名单只有 ``file,crypto,data``, 拉 http
        分片会被直接拒绝(``Protocol 'http' not on whitelist``)。改成走本地
        http 后白名单里就有 http, 分片再经 /p/ 代理转发即可正常播放。
        """
        token = parsed.path[len("/mem/"):].rsplit(".", 1)[0]
        store = getattr(self.server, "mem_store", None) or {}
        item = store.get(token)
        if not item:
            self.send_error(404, "memory playlist not found")
            return
        text, headers_json, base = item
        body = self._rewrite_m3u8(text, base, headers_json).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        html = _PLAYER_HTML.replace("__HLS_JS__", self.server.hls_js)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, qs):
        target = (qs.get("u") or [""])[0]
        headers_json = (qs.get("h") or ["{}"])[0]
        if not target:
            self.send_error(400, "missing u")
            return
        try:
            extra = json.loads(headers_json)
            if not isinstance(extra, dict):
                extra = {}
        except Exception:
            extra = {}
        req_headers = {"User-Agent": DEFAULT_UA}
        for k, v in extra.items():
            lk = k.lower()
            if lk == "host":
                continue
            # Accept-Language 是"噪音头": 部分 CDN 做请求头集合指纹校验,
            # 多这一个就 403(详见 reqhdr 文档)。分片请求没理由带它。
            if lk == "accept-language":
                continue
            req_headers[k] = str(v)
        try:
            upstream = _get_retry(target, req_headers)
        except Exception as e:
            self.send_error(502, f"upstream error: {e}")
            return
        try:
            raw = upstream.content
            up_ctype = upstream.headers.get("Content-Type", "")
            # 不只看扩展名: 部分站点清单地址无 .m3u8 后缀(接口下发),
            # 也按 Content-Type / 内容特征(#EXTM3U) 判定
            head = raw[:16].lstrip() if raw else b""
            is_m3u8 = (
                "mpegurl" in up_ctype.lower()
                or head.startswith(b"#EXTM3U")
                or target.lower().split("?")[0].endswith(".m3u8")
            )
            if is_m3u8:
                try:
                    text = raw.decode("utf-8", errors="replace")
                except Exception:
                    text = ""
                # 用重定向后的最终地址做 base: 源站常把清单 302 到签名地址,
                # 用请求前地址重写相对分片就会拼错
                base = getattr(upstream, "url", "") or target
                text = self._rewrite_m3u8(text, base, headers_json)
                body = text.encode("utf-8")
                ctype = "application/vnd.apple.mpegurl"
            else:
                body = raw
                ctype = up_ctype or "video/mp2t"
            self.send_response(upstream.status_code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        finally:
            upstream.close()

    def _rewrite_m3u8(self, text: str, base_url: str, headers_json: str) -> str:
        """把 m3u8 内的分片/KEY/MAP 地址改写为本地代理地址。"""
        from urllib.parse import urljoin

        def proxy_url(abs_url: str) -> str:
            return ("/p/" + urllib.parse.quote(_url_basename(abs_url), safe="")
                    + "?u=" + urllib.parse.quote(abs_url, safe="")
                    + "&h=" + urllib.parse.quote(headers_json, safe=""))

        lines = []
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                lines.append(proxy_url(urljoin(base_url, s)))
            elif s.startswith(("#EXT-X-KEY", "#EXT-X-MAP", "#EXT-X-MEDIA",
                                "#EXT-X-I-FRAME-STREAM-INF")):
                def _repl(m):
                    uri = m.group(3)
                    if not uri:
                        return m.group(0)
                    abs_url = uri if uri.startswith(("http://", "https://")) \
                        else urljoin(base_url, uri)
                    return m.group(1) + '"' + proxy_url(abs_url) + '"'
                lines.append(_URI_ATTR_RE.sub(_repl, line))
            else:
                lines.append(line)
        return "\n".join(lines)


class PreviewServer:
    def __init__(self, hls_js_path: Path):
        js = ""
        try:
            js = hls_js_path.read_text(encoding="utf-8")
        except Exception:
            pass
        # 防止内嵌 JS 中出现 </script> 提前闭合
        self.hls_js = js.replace("</script", "<\\/script")
        self._mem: dict = {}      # token -> (m3u8 文本, 请求头 JSON, 基准地址)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._server:
            return
        handler = _Handler
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.hls_js = self.hls_js
        self._server.mem_store = self._mem
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.5}, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
            self._thread = None

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    def proxy_url(self, stream_url: str, headers: Optional[dict] = None) -> str:
        """目标流的本地代理地址(QMediaPlayer / FFmpeg 直接播放用)。

        走本地代理而非远端直连的原因: 代理会代填 Referer/Cookie/UA 等请求头,
        并重写 m3u8 内的分片地址, 避免防盗链与跨域问题。
        """
        h = json.dumps(headers or {}, ensure_ascii=False)
        return (f"http://127.0.0.1:{self.port}/p/"
                f"{urllib.parse.quote(_url_basename(stream_url), safe='')}"
                f"?u={urllib.parse.quote(stream_url, safe='')}"
                f"&h={urllib.parse.quote(h, safe='')}")

    def memory_url(self, text: str, headers: Optional[dict] = None,
                   base: str = "") -> str:
        """把「只存在于内存里的 m3u8 原文」变成一个可播放的本地 http 地址。

        分片地址会被改写成 /p/ 代理地址(带请求头), 因此分片防盗链也能过。
        """
        import uuid

        token = uuid.uuid4().hex[:16]
        self._mem[token] = (text, json.dumps(headers or {}, ensure_ascii=False),
                            base or "http://local/")
        # 只保留最近若干份, 避免长时间使用后无限增长
        while len(self._mem) > 8:
            self._mem.pop(next(iter(self._mem)))
        return f"http://127.0.0.1:{self.port}/mem/{token}.m3u8"
