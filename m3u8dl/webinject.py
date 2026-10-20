"""浏览器嗅探的页面注入脚本 —— 补足 QtWebEngine 的能力短板。

## 为什么需要注入(实测根因)

PySide6 自带的 QtWebEngine 是开源编译版, **不含 H.264/AAC 专有编解码器**
(专利限制)。实测结果:

    MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E"')  -> false
    MediaSource.isTypeSupported('audio/mp4; codecs="mp4a.40.2"')    -> false
    document.createElement('video').canPlayType('...mpegurl')       -> ""

绝大多数直播站(Stripchat 等)的视频是 H.264 + AAC, 站点播放器(hls.js 等)
在启动阶段就会做能力检测 `Hls.isSupported()`, 检测失败后**直接放弃** ——
于是既不会发出 m3u8 请求(嗅探拦截为空), 页面又显示
"This video file cannot be played. (Error Code: 102630)"。

## 解决思路

在页面脚本执行前(DocumentCreation)把 avc1 / mp4a 报告为"支持", 播放器
据此正常发起 m3u8 / 分片请求 —— 网络请求拦截即可捕获地址。**下载由本软件
自己的下载引擎完成, 完全不需要浏览器解码**, 因此伪装不会影响最终结果;
只有"页面内直接播放"这一项受引擎限制无法解决(预览改用 QMediaPlayer)。

## 第二职责: URL 捕获

额外钩住 XMLHttpRequest / fetch / video.src, 把地址存入
`window.__M3U8DL_URLS`。这是网络请求拦截的补充, 可覆盖拦截器可能遗漏的
情况(Worker 内请求、无扩展名清单地址、由 JS 拼接后才交给播放器的地址)。

## 第三职责: 主动起播(uqload 家族)

另一类站点(实测 `livecamrips.to` 的 `mdzsmutpcvykb.net` / `mxdrop.click`
iframe)把视频写成 `<video preload="none">`, 地址藏在 packer 混淆的
`MDCore.wurl` 里, **必须点击播放才发请求**。而 `runJavaScript` 只能操作
主框架, 点不进跨域 iframe —— 嗅探结果就是 0 条, 但猫抓在真浏览器里能抓到
(用户真点了播放)。故注入脚本带 `runsOnSubFrames=True`, 在 iframe 内部
静音主动 `play()` 并点击 video.js 的大播放按钮, 把请求逼出来。
"""
from __future__ import annotations

# 需要伪装为"支持"的编解码器特征
_CODEC_OK_RE = r"avc1|avc3|h264|mp4a|aac|mp4v|mp2t|avc"

# ---------------------------------------------------------------- 注入脚本
INJECT_JS = r"""
(function(){
  if (window.__m3u8dl_installed) { return; }
  window.__m3u8dl_installed = true;

  // ===== 1. 编解码器能力伪装(QtWebEngine 缺 H.264/AAC) =====
  var CODEC_OK = new RegExp("__CODEC_RE__", "i");
  function claimOk(type) {
    try {
      if (typeof type !== "string") { return false; }
      return CODEC_OK.test(type);
    } catch (e) { return false; }
  }

  try {
    if (window.MediaSource && MediaSource.isTypeSupported) {
      var _mseOrig = MediaSource.isTypeSupported.bind(MediaSource);
      MediaSource.isTypeSupported = function (t) {
        if (claimOk(t)) { return true; }
        try { return _mseOrig(t); } catch (e) { return false; }
      };
    }
  } catch (e) {}

  try {
    var _cptOrig = HTMLMediaElement.prototype.canPlayType;
    HTMLMediaElement.prototype.canPlayType = function (t) {
      if (claimOk(t)) { return "probably"; }
      try { return _cptOrig.call(this, t); } catch (e) { return ""; }
    };
  } catch (e) {}

  // ===== 2. URL 捕获(补充网络请求拦截) =====
  var seen = {};
  window.__M3U8DL_URLS = [];
  function rec(u) {
    try {
      if (!u || typeof u !== "string") { return; }
      // 页面常用相对路径(/stream/x.m3u8), 先解析成绝对地址再判断
      try { u = new URL(u, location.href).href; } catch (e) { return; }
      if (!/^https?:/i.test(u)) { return; }
      if (seen[u]) { return; }
      seen[u] = 1;
      window.__M3U8DL_URLS.push(u);
      try { console.log("[M3U8DL_URL] " + u); } catch (e) {}
    } catch (e) {}
  }
  window.__m3u8dl_rec = rec;

  try {
    var _xo = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (m, u) {
      rec(u);
      return _xo.apply(this, arguments);
    };
  } catch (e) {}

  try {
    if (window.fetch) {
      var _f = window.fetch;
      window.fetch = function (input) {
        try {
          rec(typeof input === "string" ? input : (input && input.url));
        } catch (e) {}
        return _f.apply(this, arguments);
      };
    }
  } catch (e) {}

  try {
    var _desc = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "src");
    if (_desc && _desc.set) {
      Object.defineProperty(HTMLMediaElement.prototype, "src", {
        configurable: true,
        get: _desc.get,
        set: function (v) { try { rec(v); } catch (e) {} return _desc.set.call(this, v); }
      });
    }
  } catch (e) {}

  // 扫描 <video>/<source> 上的地址
  function scanDom() {
    try {
      var els = document.querySelectorAll("video, source");
      for (var i = 0; i < els.length; i++) {
        rec(els[i].src || "");
        rec(els[i].currentSrc || "");
      }
    } catch (e) {}
  }

  // ===== 3. 主动起播(关键) =====
  // 大量播放器把 <video preload="none"> 写死, 必须**用户点击**才去拉流。
  // 而这条路在内置引擎里走不通: runJavaScript 只能操作主框架, 点不进跨域
  // iframe —— 于是播放器永远不请求视频, 嗅探结果就是 0 条(猫抓在真浏览器里
  // 能抓到, 是因为用户真的点了播放)。
  //
  // 这段脚本 runsOnSubFrames=True, 所以能在 iframe 内部执行: 静音 + 主动
  // play(), 让播放器走正常流程把地址请求发出去。**我们只要请求, 不要画面**
  // (开源引擎本来就解不了 H.264), 所以静音自动播放没有副作用。
  function pokePlay() {
    try {
      var vs = document.querySelectorAll("video, audio");
      for (var i = 0; i < vs.length; i++) {
        var v = vs[i];
        try {
          if (v.readyState === 0 || v.paused) {
            v.muted = true;
            v.setAttribute("muted", "");
            v.setAttribute("autoplay", "");
            if (v.preload === "none" || !v.preload) { v.preload = "auto"; }
            var p = v.play();
            if (p && p.catch) { p.catch(function () {}); }
          }
        } catch (e) {}
      }
      // video.js 的巨型播放按钮: 它才是真正发起加载的入口
      var btns = document.querySelectorAll(
        ".vjs-big-play-button, .jw-icon-display, .vp-play, " +
        "[class*=big-play], [class*=play-button], [id*=playbutton]");
      for (var k = 0; k < Math.min(btns.length, 3); k++) {
        try {
          if (btns[k].offsetParent !== null) { btns[k].click(); }
        } catch (e) {}
      }
    } catch (e) {}
  }

  try {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", scanDom);
    } else { scanDom(); }
    setInterval(scanDom, 2500);
    // 起播要早、要勤: 播放器可能在 DOMContentLoaded 之后才注入 video.js
    setInterval(pokePlay, 1200);
    [800, 2000, 4000, 7000].forEach(function (t) {
      setTimeout(pokePlay, t);
    });
  } catch (e) {}
})();
"""

# 读取页面已捕获地址
READ_JS = "JSON.stringify(window.__M3U8DL_URLS || [])"

SCRIPT_NAME = "m3u8dl_sniff_inject"


def build_source() -> str:
    """生成注入脚本源码(替换编解码器特征正则)。"""
    return INJECT_JS.replace("__CODEC_RE__", _CODEC_OK_RE)


def install(profile) -> bool:
    """把注入脚本装到 QWebEngineProfile(页面脚本执行前生效)。

    返回是否安装成功。需在 QApplication 创建后调用。
    """
    try:
        from PySide6.QtWebEngineCore import QWebEngineScript

        script = QWebEngineScript()
        script.setName(SCRIPT_NAME)
        script.setSourceCode(build_source())
        script.setInjectionPoint(
            QWebEngineScript.InjectionPoint.DocumentCreation)
        # MainWorld: 与页面脚本同一 JS 世界, 否则无法覆盖页面的 API
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)

        scripts = profile.scripts()
        # 移除同名旧脚本(重复安装时避免叠加)
        try:
            for old in list(scripts.toList()):
                if old.name() == SCRIPT_NAME:
                    scripts.remove(old)
        except Exception:
            pass
        scripts.insert(script)
        return True
    except Exception:
        return False
