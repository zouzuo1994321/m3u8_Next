"""深度搜索 —— 逆向自猫抓 cat-catch ``catch-script/search.js``。

和普通嗅探的本质区别
--------------------
普通嗅探盯着**网络**：请求了哪个 URL。站点只要不把地址放进网络请求就抓不到：

- 地址在 JS 里算出来，直接 `URL.createObjectURL(new Blob([m3u8Text]))` 交给播放器
- 分片地址在 Worker 里拼装
- m3u8 文本由 `String.fromCharCode` 逐字拼出
- KEY 直接写在 JS 里 / 由 32 位 hex 或 24 位 base64 字符串承载

深度搜索盯着**内存**：在页面里挂钩那些「把地址/文本/密钥算出来」的函数，
从数据流里把目标捞出来。对应原实现 search.js 的以下钩子（逐个移植）：

============================  ==========================================
钩子对象                       抓什么
============================  ==========================================
``Worker``                     把自身注入 Worker，抓 Worker 内产生的地址
``JSON.parse``                 递归遍历解析结果，找 URL / m3u8 文本 / 疑似 KEY
``XMLHttpRequest``             response 文本、json、arraybuffer(16/32B KEY)
``fetch``                      response 文本 / json / m3u8
``TextDecoder.prototype.decode`` 解码出的 m3u8 文本
``btoa`` / ``atob``            24 位 base64 KEY、base64 的 m3u8 / MPD
``String.fromCharCode``        逐字拼装的 m3u8、32 位 hex KEY
``DataView`` / TypedArray      16 字节 KEY（含 32/128/256 字节重复填充还原）
``Array.prototype.join``       数组拼出的 m3u8 / 24 位 base64 KEY
``Array.prototype.slice``      32 字节数组切出 16 字节 KEY
``String.prototype.indexOf``   `'#EXTM3U'` 命中时的整段文本
``<script>`` 静态扫描          源码里写死的地址
============================  ==========================================

与原实现的差异
--------------
原实现抓到 m3u8 文本后 `URL.createObjectURL` 造一个 blob 地址丢给下载器。
blob 地址有生命周期问题（页面跳转即失效），且后续还要再解析一次。
这里改为**把 m3u8 原文直接回传 Python**，由本地解析器处理，链路更短更稳。
"""
from __future__ import annotations

SCRIPT_NAME = "m3u8dl-deepsearch"

#: 结果读取（executeJavaScript 返回 JSON 字符串）
READ_JS = (
    "(function(){var d=window.__M3U8DL_DEEP;"
    "if(!d){return '';}"
    "return JSON.stringify({urls:d.urls,keys:d.keys,texts:d.texts});})()"
)

#: 清空结果
CLEAR_JS = (
    "(function(){var d=window.__M3U8DL_DEEP;"
    "if(!d){return false;}"
    "d.urls=[];d.keys=[];d.texts=[];return true;})()"
)

_JS = r"""
(function () {
  if (window.__M3U8DL_DEEP_READY) { return; }
  window.__M3U8DL_DEEP_READY = true;

  var D = window.__M3U8DL_DEEP = { urls: [], keys: [], texts: [] };
  if (!window.__M3U8DL_URLS) { window.__M3U8DL_URLS = []; }

  var seenUrl = Object.create(null);
  var seenKey = Object.create(null);
  var seenText = Object.create(null);
  var MAX_URLS = 400, MAX_KEYS = 300, MAX_TEXTS = 60;

  var reUrlLike = /\.(m3u8|m3u|mpd|mp4|flv|mp3|ts|key)(\?|#|$)/i;
  var reHex32 = /^[0-9a-f]{32}$/i;
  var reB64_16 = /^[A-Za-z0-9+/]{22}==$/;
  var reDataUri = /^data:(application|video|audio)\//i;
  var reMpd = /urn:mpeg:dash:schema:mpd/i;
  var _fromCharCode = String.fromCharCode;
  var _btoa = window.btoa;
  var _atob = window.atob;
  var _U8 = Uint8Array;
  // 原生引用: 后面的钩子会替换 String.prototype.indexOf / Array.prototype.join,
  // 钩子内部判定必须走原生版本, 否则会把自己产生的数据再喂回自己。
  var _strIndexOf = String.prototype.indexOf;
  var _strToUpper = String.prototype.toUpperCase;
  var _strSlice = String.prototype.slice;

  function hasMarker(s, marker) { return _strIndexOf.call(s, marker) >= 0; }

  // 原生 toString: 给被替换的函数做一个"看起来还是原生的"toString。
  // 注意不能用 fn.toString 去取原函数的 toString —— 替换后它们指向同一对象,
  // 会自我递归到爆栈。
  var _fnToString = Function.prototype.toString;
  function mask(fn, orig) {
    try {
      Object.defineProperty(fn, "toString", {
        value: function () { return _fnToString.call(orig); },
        configurable: true, writable: true
      });
    } catch (e) {}
  }

  function str(v) { try { return String(v); } catch (e) { return ""; } }
  function pushUrl(u, from) {
    if (!u || D.urls.length >= MAX_URLS) { return; }
    u = str(u).trim();
    if (!/^https?:\/\//i.test(u)) { return; }
    if (seenUrl[u]) { return; }
    seenUrl[u] = 1;
    D.urls.push({ u: u, from: from || "" });
    window.__M3U8DL_URLS.push(u);   // seenUrl 已保证去重
  }
  function pushKey(k, type, from) {
    if (!k || D.keys.length >= MAX_KEYS) { return; }
    k = str(k);
    var id = type + ":" + k;
    if (seenKey[id]) { return; }
    seenKey[id] = 1;
    D.keys.push({ k: k, t: type, from: from || "" });
  }
  function pushText(t, from) {
    if (!t || D.texts.length >= MAX_TEXTS) { return; }
    t = str(t);
    if (t.length < 16 || t.length > 4 * 1024 * 1024) { return; }
    var up = t.slice(0, 512).toUpperCase();
    var h = t.length + ":" + t.slice(0, 64);
    if (seenText[h]) { return; }
    seenText[h] = 1;
    D.texts.push({ t: t, from: from || "" });
    var m = t.match(/(https?:\/\/[^\s"'<>]+)/);
    if (m) { pushUrl(m[1], "m3u8文本"); }
  }

  // ---- 字节 -> base64（16 字节 KEY 规范化） ----
  function bytesToB64(view) {
    try {
      var s = "";
      for (var i = 0; i < view.length; i++) { s += _fromCharCode(view[i]); }
      return _btoa(s);
    } catch (e) { return ""; }
  }
  function takeKeyBytes(buf, from) {
    try {
      var u8 = new _U8(buf);
      if (u8.length === 16) { pushKey(bytesToB64(u8), "raw16", from); return; }
      // 32/128/256 字节: 可能是同一个 16 字节重复填充
      if ((u8.length === 32 || u8.length === 128 || u8.length === 256)
          && u8.length % 16 === 0) {
        var same = true;
        for (var i = 16; i < u8.length; i++) {
          if (u8[i] !== u8[i - 16]) { same = false; break; }
        }
        if (same) { pushKey(bytesToB64(u8.subarray(0, 16)), "raw16rep", from); return; }
        if (u8.length === 32) {
          pushKey(bytesToB64(u8.subarray(0, 16)), "raw16head", from);
          pushKey(bytesToB64(u8.subarray(16, 32)), "raw16tail", from);
        }
      }
    } catch (e) {}
  }
  function checkStrKey(s, from) {
    s = str(s);
    if (!s) { return; }
    // 32 位 hex
    if (s.length === 32 && reHex32.test(s)) { pushKey(s, "hex32", from); return; }
    // 24 位 base64（16 字节）
    if (s.length === 24 && reB64_16.test(s)) { pushKey(s, "b64_16", from); }
  }
  function isM3U8(t) {
    if (!t || typeof t !== "string") { return false; }
    var head = _strToUpper.call(_strSlice.call(t, 0, 512));
    return hasMarker(head, "#EXTM3U");
  }

  // ---- 递归翻查任意对象（对应原 findMedia） ----
  function walk(data, depth, from) {
    if (!data || depth > 20) { return; }
    try {
      if (data instanceof Array) {
        if (data.length === 16) {
          var isKey = true;
          for (var i = 0; i < 16; i++) {
            if (typeof data[i] !== "number" || data[i] > 255) { isKey = false; break; }
          }
          if (isKey) {
            takeKeyBytes(new _U8(data).buffer, from);
            return;
          }
        }
      }
      if (typeof ArrayBuffer !== "undefined" && data instanceof ArrayBuffer) {
        if (data.byteLength === 16 || data.byteLength === 32
            || data.byteLength === 128 || data.byteLength === 256) {
          takeKeyBytes(data, from);
        }
        return;
      }
    } catch (e) {}

    for (var k in data) {
      var v;
      try { v = data[k]; } catch (e) { continue; }
      if (v === null || v === undefined) { continue; }
      if (typeof v === "object") {
        if (v instanceof Array && v.length === 16) {
          var allNum = true;
          for (var j = 0; j < 16; j++) {
            if (typeof v[j] !== "number" || v[j] > 255) { allNum = false; break; }
          }
          if (allNum) { takeKeyBytes(new _U8(v).buffer, from); continue; }
        }
        walk(v, depth + 1, from);
        continue;
      }
      if (typeof v !== "string") { continue; }
      if (/^(https?:)?\/\//i.test(v) && reUrlLike.test(v)) {
        pushUrl(v.indexOf("//") === 0 ? (location.protocol + v) : v, from);
        continue;
      }
      if (isM3U8(v)) { pushText(v, from); continue; }
      if (reDataUri.test(v.slice(0, 17))) { decodeDataUri(v, from); continue; }
      if (reMpd.test(v)) { pushText(v, from); continue; }
      checkStrKey(v, from);
    }
  }

  // ---- data: URI 里的 m3u8 ----
  function decodeDataUri(text, from) {
    try {
      var s = str(text);
      var comma = s.indexOf(",");
      if (comma < 0) { return; }
      var head = s.slice(0, comma).toLowerCase();
      var body = s.slice(comma + 1);
      var out = head.indexOf("base64") >= 0 ? _atob(body)
        : decodeURIComponent(body.replace(/\+/g, " "));
      if (isM3U8(out)) { pushText(out, from || "data-uri"); }
    } catch (e) {}
  }

  // ================= 钩子 1: JSON.parse =================
  try {
    var _parse = JSON.parse;
    JSON.parse = function () {
      var r = _parse.apply(this, arguments);
      try { walk(r, 0, "JSON.parse"); } catch (e) {}
      return r;
    };
    JSON.parse.toString = function () { return _parse.toString(); };
  } catch (e) {}

  // ================= 钩子 2: XHR =================
  try {
    var _open = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method) {
      var self = this;
      try {
        self.addEventListener("readystatechange", function () {
          if (self.status !== 200) { return; }
          try {
            var u = self.responseURL || "";
            if (u) { pushUrl(u, "XHR"); }
            if (self.responseType === "arraybuffer" && self.response
                && self.response.byteLength) {
              takeKeyBytes(self.response, "XHR.arraybuffer");
              return;
            }
            if (typeof self.response === "object") { walk(self.response, 0, "XHR.json"); return; }
            if (typeof self.response !== "string" || !self.response) { return; }
            if (isM3U8(self.response)) { pushText(self.response, "XHR"); return; }
            if (reDataUri.test(self.response.slice(0, 17))) { decodeDataUri(self.response, "XHR"); return; }
            if (/^[\s]*[{[]/.test(self.response) && self.response.length < 4 * 1024 * 1024) {
              walk(self.response, 0, "XHR字符串");
            }
            checkStrKey(self.response, "XHR");
          } catch (e) {}
        });
      } catch (e) {}
      return _open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.open.toString = function () { return _open.toString(); };
  } catch (e) {}

  // ================= 钩子 3: fetch =================
  try {
    var _fetch = window.fetch;
    if (typeof _fetch === "function") {
      window.fetch = function (input, init) {
        var url = typeof input === "string" ? input : (input && input.url) || "";
        if (url) { pushUrl(url, "fetch"); }
        var p = _fetch.apply(this, arguments);
        try {
          p.then(function (resp) {
            try {
              if (resp && resp.url) { pushUrl(resp.url, "fetch.response"); }
              var ct = "";
              try { ct = resp.headers.get("content-type") || ""; } catch (e) {}
              if (!/json|mpegurl|text|javascript|octet/i.test(ct)) { return; }
              resp.clone().text().then(function (t) {
                try {
                  if (isM3U8(t)) { pushText(t, "fetch"); return; }
                  if (/^\s*[{[]/.test(t) && t.length < 4 * 1024 * 1024) { walk(t, 0, "fetch文本"); }
                } catch (e) {}
              }).catch(function () {});
            } catch (e) {}
          });
        } catch (e) {}
        return p;
      };
      window.fetch.toString = function () { return _fetch.toString(); };
    }
  } catch (e) {}

  // ================= 钩子 4: TextDecoder =================
  try {
    var _decode = TextDecoder.prototype.decode;
    TextDecoder.prototype.decode = function () {
      var r = _decode.apply(this, arguments);
      try { if (isM3U8(r)) { pushText(r, "TextDecoder"); } } catch (e) {}
      return r;
    };
    TextDecoder.prototype.decode.toString = function () { return _decode.toString(); };
  } catch (e) {}

  // ================= 钩子 5: btoa / atob =================
  try {
    window.btoa = function (data) {
      var out = _btoa.apply(this, arguments);
      try {
        checkStrKey(out, "btoa");
        if (isM3U8(data)) { pushText(data, "btoa"); }
      } catch (e) {}
      return out;
    };
    window.btoa.toString = function () { return _btoa.toString(); };
  } catch (e) {}
  try {
    window.atob = function (data) {
      var out = _atob.apply(this, arguments);
      try {
        checkStrKey(data, "atob");
        if (isM3U8(out)) { pushText(out, "atob"); }
        else if (out && out.length > 6 && out[out.length - 6] === "<") { pushText(out, "atob.mpd"); }
      } catch (e) {}
      return out;
    };
    window.atob.toString = function () { return _atob.toString(); };
  } catch (e) {}

  // ================= 钩子 6: String.fromCharCode =================
  var m3u8Buf = "";
  try {
    String.fromCharCode = new Proxy(_fromCharCode, {
      apply: function (target, thisArg, args) {
        var out = Reflect.apply(target, thisArg, args);
        try {
          if (out && out.length >= 7) {
            var isHead = out.slice(0, 7) === "#EXTM3U";
            // 播放器可能整段解码, 也可能逐行解码。逐行时 URL 行既不以
            // "#EXTM3U" 开头也不含 "#EXTINF:", 必须靠"续接模式"补进来,
            // 否则拼出来的清单只有标签、没有分片地址。
            var isTag = out.charAt(0) === "#";
            var isUrlLine = /^[^\s"'<>]{4,}$/.test(out.trim());
            if (isHead) {
              m3u8Buf = out;
            } else if (m3u8Buf && (isTag || isUrlLine)) {
              m3u8Buf += out;
            } else if (hasMarker(out, "#EXTINF:")) {
              m3u8Buf = out;
            } else {
              var k = out.replace(/\u0010/g, "");
              if (k.length === 32 && reHex32.test(k)) {
                pushKey(k, "hex32", "fromCharCode");
              }
              return out;
            }
            if (m3u8Buf.length > 4 * 1024 * 1024) { m3u8Buf = m3u8Buf.slice(-1024); }
            if (hasMarker(m3u8Buf, "#EXT-X-ENDLIST")) {
              pushText(m3u8Buf, "fromCharCode");
              m3u8Buf = "";
            } else if (m3u8Buf.length > 8192) {
              pushText(m3u8Buf, "fromCharCode(流式)");
            }
          }
        } catch (e) {}
        return out;
      }
    });
    try { mask(String.fromCharCode, _fromCharCode); } catch (e) {}
  } catch (e) {}

  // ================= 钩子 7: DataView =================
  try {
    var _DV = DataView;
    var DVProxy = function () {
      var inst = new _DV(...arguments);
      try {
        if (inst.byteLength === 16) { takeKeyBytes(inst.buffer, "DataView"); }
        else if (inst.byteLength === 32 || inst.byteLength === 128
                 || inst.byteLength === 256) { takeKeyBytes(inst.buffer, "DataView"); }
      } catch (e) {}
      return inst;
    };
    DVProxy.prototype = _DV.prototype;
    try { DVProxy.toString = function () { return _DV.toString(); }; } catch (e) {}
    window.DataView = DVProxy;
  } catch (e) {}

  // ================= 钩子 8: TypedArray 构造 =================
  function wrapTyped(name) {
    try {
      var Orig = window[name];
      if (!Orig) { return; }
      var P = new Proxy(Orig, {
        construct: function (target, args) {
          var inst = new target(...args);
          try {
            if (args[0] instanceof ArrayBuffer) {
              if (args[0].byteLength === 16) { takeKeyBytes(args[0], name); }
            } else if (typeof args[0] === "number") {
              if (args[0] === 16) { takeKeyBytes(inst.buffer, name); }
            } else if (inst && inst.buffer && inst.buffer.byteLength === 16) {
              takeKeyBytes(inst.buffer, name);
            }
          } catch (e) {}
          return inst;
        }
      });
      window[name] = P;
    } catch (e) {}
  }
  wrapTyped("Uint8Array");
  wrapTyped("Uint16Array");
  wrapTyped("Uint32Array");
  wrapTyped("Int8Array");
  wrapTyped("Int16Array");
  wrapTyped("Int32Array");

  // ================= 钩子 9: Array.join / slice =================
  try {
    var _join = Array.prototype.join;
    Array.prototype.join = function () {
      var out = _join.apply(this, arguments);
      try {
        if (isM3U8(out)) { pushText(out, "Array.join"); }
        else if (out && out.length === 24) { checkStrKey(out, "Array.join"); }
      } catch (e) {}
      return out;
    };
    Array.prototype.join.toString = function () { return _join.toString(); };
  } catch (e) {}
  try {
    var _slice = Array.prototype.slice;
    Array.prototype.slice = function (start, end) {
      var out = _slice.apply(this, arguments);
      try {
        if (end === 16 && this.length === 32) {
          var ok = true;
          for (var i = 0; i < out.length; i++) {
            if (typeof out[i] !== "number" || out[i] > 255) { ok = false; break; }
          }
          if (ok) { takeKeyBytes(new _U8(out).buffer, "Array.slice"); }
        }
      } catch (e) {}
      return out;
    };
    Array.prototype.slice.toString = function () { return _slice.toString(); };
  } catch (e) {}

  // ================= 钩子 10: String.indexOf('#EXTM3U') =================
  try {
    var _indexOf = String.prototype.indexOf;
    String.prototype.indexOf = function (needle, from) {
      var out = _indexOf.apply(this, arguments);
      try {
        if (needle === "#EXTM3U" && out !== -1 && this.length < 4 * 1024 * 1024) {
          pushText(this.substring(from || 0), "String.indexOf");
        }
      } catch (e) {}
      return out;
    };
    String.prototype.indexOf.toString = function () { return _indexOf.toString(); };
  } catch (e) {}

  // ================= 钩子 11: Worker 内注入 =================
  try {
    var _Worker = window.Worker;
    if (typeof _Worker === "function") {
      var selfSource = null;
      window.Worker = function (scriptURL, options) {
        try {
          var u = String(scriptURL);
          if (/^(blob|data):/i.test(u)) { return new _Worker(scriptURL, options); }
          var xhr = new XMLHttpRequest();
          xhr.open("GET", u, false);
          xhr.send();
          if (xhr.status === 200 && xhr.responseText) {
            var blob = new Blob([__SELF_SRC__, "\n", xhr.responseText],
                                { type: "text/javascript" });
            var w = new _Worker(URL.createObjectURL(blob), options);
            w.addEventListener("message", function (ev) {
              try {
                var d = ev.data;
                if (d && d.__m3u8dlDeep) {
                  if (d.u) { pushUrl(d.u, "Worker"); }
                  if (d.t) { pushText(d.t, "Worker"); }
                  if (d.k) { pushKey(d.k, d.kt || "raw", "Worker"); }
                }
              } catch (e) {}
            });
            return w;
          }
        } catch (e) {}
        return new _Worker(scriptURL, options);
      };
      try { window.Worker.toString = function () { return _Worker.toString(); }; } catch (e) {}
    }
  } catch (e) {}

  // ================= 钩子 12: <script> 静态扫描 =================
  try {
    var scan = function () {
      var re = /["']((?:(?:https?:)?\/\/)?[^"'\s]*?\.(?:m3u8|mp4|flv|mpd)(?:\?[^"'\s]*)?)["']/gi;
      document.querySelectorAll("script:not([src])").forEach(function (s) {
        var t = s.textContent;
        if (!t) { return; }
        var m;
        while ((m = re.exec(t)) !== null) {
          var u = (m[1] || "").replace(/['"]/g, "").trim();
          if (u && !/^https?:/i.test(u)) { u = location.protocol + "//" + u.replace(/^\/\//, ""); }
          pushUrl(u, "script源码");
        }
      });
    };
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", scan);
    } else { scan(); }
  } catch (e) {}
})();
"""


def build_script() -> str:
    """生成注入脚本。

    ``__SELF_SRC__`` 是 Worker 钩子用来「把自身注入 Worker」的源码。原实现靠先
    注入一个变量、再靠注入顺序保证它存在；这里改成**自包含**：脚本开头先把自身
    正文写成字符串挂到 ``window.__M3U8DL_DEEP_SRC``，彻底摆脱注入顺序依赖。

    末尾用 ``try{}catch(e){}`` 包住：Worker 内没有 ``window``，脚本在那儿会抛
    ReferenceError，包起来才不会连带中断 Worker 自身的代码。
    """
    import json

    body = _JS.replace("__SELF_SRC__", "String(window.__M3U8DL_DEEP_SRC||'')")
    literal = json.dumps(body)
    return ("(function(){try{window.__M3U8DL_DEEP_SRC=" + literal
            + ";}catch(e){}})();\ntry{\n" + body + "\n}catch(e){}")


def install(profile) -> bool:
    """挂到 profile（DocumentCreation + MainWorld + 子框架）。"""
    from PySide6.QtWebEngineCore import QWebEngineScript

    src = build_script()
    try:
        script = QWebEngineScript()
        script.setName(SCRIPT_NAME)
        script.setSourceCode(src)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        scripts = profile.scripts()
        for old in list(scripts.toList()):
            if old.name() == SCRIPT_NAME:
                scripts.remove(old)
        scripts.insert(script)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 结果解析
def parse_result(raw: str) -> dict:
    """把 READ_JS 返回的 JSON 解析成 ``{urls, keys, texts}``。

    对元素形态宽容：既接受页面返回的 ``{"u": ...}`` / ``{"t": ...}`` 结构，
    也接受直接就是字符串的写法（便于外部把结果塞进来测试或续传）。
    """
    import json

    empty = {"urls": [], "keys": [], "texts": []}
    if not raw:
        return empty
    try:
        data = json.loads(raw)
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty

    def _pick(items, key):
        out = []
        for x in items or []:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict) and x.get(key):
                out.append(str(x[key]))
        return out

    keys = [k for k in (data.get("keys") or []) if isinstance(k, dict)]
    return {
        "urls": _pick(data.get("urls"), "u"),
        "keys": keys,
        "texts": _pick(data.get("texts"), "t"),
    }


def key_candidates(keys: list) -> list:
    """把捕获的疑似 KEY 规范成 16 字节 bytes 列表（去重保序）。

    - ``hex32``：32 位十六进制 -> 16 字节
    - ``b64_16``：24 位 base64（``==`` 结尾）-> 16 字节
    - ``raw16*``：已经 base64 化的 16 字节原始数据 -> 还原
    """
    import base64
    import binascii

    out = []
    seen = set()
    for item in keys or []:
        if not isinstance(item, dict):
            continue
        k = item.get("k") or ""
        t = item.get("t") or ""
        raw = None
        try:
            if t.startswith("hex") and len(k) == 32:
                raw = binascii.unhexlify(k)
            elif t.startswith("b64") or t.startswith("raw"):
                raw = base64.b64decode(k)
            elif len(k) == 32:
                try:
                    raw = binascii.unhexlify(k)
                except Exception:
                    raw = None
        except Exception:
            raw = None
        if raw and len(raw) == 16 and raw not in seen:
            seen.add(raw)
            out.append(raw)
    return out
