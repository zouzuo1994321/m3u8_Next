"""内置广告拦截（adblock）—— 双层：网络请求拦截 + 页面元素隐藏。

为什么需要它
------------
这类聚合站（livecamrips / uqload 系）的「视频列表」里混着大量**广告视频**
（`vidwestxyz.com/.../low_640x360_1000.mp4` 这种贴片、还有各个广告联盟的
m3u8），嗅探会把它们一并收进列表，用户点到就是「预览播放失败 / 下载下来
是广告」。拦掉这些请求，列表自然干净，页面加载也快。

规则来源（对齐 GetAdBlock / uBlock Origin）
-------------------------------------------
QtWebEngine 装不了浏览器扩展，所以这里把 **EasyList + EasyList China**
（+ 中文补充）的规则**移植**成域名集合，见 ``m3u8dl/adlist.py``：
约 **5.8 万个**广告域名，由 ``tools/build_adlist.py`` 从
https://easylist.to/ 自动生成。运行期只做「后缀命中」判断，一次请求
最多几次 set 查找，不会拖慢页面。

两层策略
--------
1. **网络层**（``should_block``）：命中广告域名或路径特征就
   ``info.block(True)``，请求根本不出网。放在 `VideoUrlInterceptor` 里，
   但**视频地址优先**——先判视频特征并收集，命中视频的永不拦截，
   避免把广告联盟里的 m3u8 误伤成"拦掉就少了候选"。
2. **元素层**（``build_script``）：注入 CSS 隐藏常见广告位，并用
   MutationObserver 清掉后来的广告 iframe（弹窗广告多为动态插入）。

代价与边界
----------
- 只按**域名/路径特征**判断，不做内容分析，所以不会误杀正常视频；
- 不拦主框架导航（否则用户点进站内播放页会失败）；
- 弹窗本来就被 Qt 默认策略拦掉了（未覆写 ``createWindow`` 就不会开新窗口）。
"""
from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlparse

from . import adlist

SCRIPT_NAME = "m3u8dl_adblock"

#: 手写的补充域名(EasyList 之外, 主要针对成人站弹窗联盟 —— 这部分更新快,
#: 自动生成的清单未必及时跟上)
_EXTRA_HOSTS = (
    # ---- 弹窗 / 成人站常见 ----
    "exoclick.com", "exosrv.com", "juicyads.com", "trafficjunky.com",
    "trafficjunky.net", "popads.net", "popcash.net", "propellerads.com",
    "onclckds.com", "onclickalgo.com", "hilltopads.net", "clickadu.com",
    "a-ads.com", "adsterra.com", "adspyglass.com", "realsrv.com",
    "tsyndicate.com", "exdynsrv.com", "adnium.com", "adxmaster.net",
    "bngprm.com", "bidgear.com", "mgid.com", "revcontent.com",
    "adcash.com", "adskeeper.com", "clickaine.com", "evadav.com",
    "galaksion.com", "tsyndicate.net", "adxxx.com", "juicyads.net",
    # ---- 内容推荐 / 追踪 ----
    "taboola.com", "outbrain.com", "zergnet.com", "plista.com",
    "google-analytics.com", "googletagmanager.com", "hotjar.com",
    "clarity.ms", "sentry.io", "scorecardresearch.com",
    "quantserve.com", "moatads.com", "doubleverify.com",
)

#: 最终域名集合 = EasyList 全量 ∪ 手写补充
AD_HOSTS = frozenset(adlist.AD_DOMAINS) | frozenset(_EXTRA_HOSTS)

#: 路径 / 查询特征（只针对上面没覆盖到的小联盟）
AD_PATH_RES = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"/adserver/",
    r"/adserver\d*\.",
    r"/ads?/",
    r"/ads?[-_.]",
    r"/adverts?(/|[-_.])",
    r"/adv/",
    r"/banners?/",
    r"/popunder",
    r"/pop[a-z]*\.js",
    r"/prebid",
    r"/vast\d?\.xml",
    r"/vpaid",
    r"/pb\.js",
    r"[?&]ad_?zone=",
    r"[?&]adunit=",
    r"[?&]click_?id=",
    r"[?&]utm_source=ad",
))

#: 命中这些就是视频本身 —— 无论域名如何都不拦截
_VIDEO_KEEP_RE = re.compile(
    r"\.m3u8(\?|#|$)|\.mp4(\?|#|$)|\.flv(\?|#|$)|\.webm(\?|#|$)",
    re.IGNORECASE,
)

#: 不拦截的资源类型（主框架/子框架导航：拦掉会把页面搞坏）
_KEEP_TYPES = ("ResourceTypeMainFrame", "ResourceTypeNavigation")


def _host_match(host: str, domains: Iterable[str] = AD_HOSTS) -> bool:
    """域名后缀命中判断。

    域名集合有 5.8 万条, **不能线性遍历**(每个请求走一遍就是几万次比较)。
    改为把 host 逐级剥掉最左标签去 set 里查:
    ``a.b.example.com`` 只需查 3 次, 与集合大小无关。
    """
    h = (host or "").lower().strip(".")
    if not h:
        return False
    parts = h.split(".")
    # 逐级剥离最左标签, 最后一级保留 "example.com"(单段不参与判断)
    for i in range(max(0, len(parts) - 1)):
        cand = ".".join(parts[i:])
        if cand in domains:
            return True
    return False


def should_block(url: str, resource_type: str = "") -> bool:
    """判断该请求是否应被拦截（视频地址与框架导航永不拦截）。"""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    if any(t in (resource_type or "") for t in _KEEP_TYPES):
        return False
    if _VIDEO_KEEP_RE.search(url):
        return False
    try:
        p = urlparse(url)
    except Exception:
        return False
    if _host_match(p.hostname or "", AD_HOSTS):
        return True
    probe = f"{p.path}?{p.query}"
    return any(r.search(probe) for r in AD_PATH_RES)


# ---------------------------------------------------------------- 元素层
#: 常见广告位选择器（保守：只挑语义明确的）
_CSS = """
.ad, .ads, .adsbox, .ad-box, .ad-container, .ad-wrapper, .ad-slot,
.advertisement, .advertising, .adsbygoogle, .ad-unit, .ads-block,
[id^="ad-"], [id^="ads-"], [id^="div-gpt-ad"], [id^="google_ads"],
[class^="ad-slot"], [class*=" ad-slot"], [data-ad-slot],
iframe[src*="doubleclick"], iframe[src*="googlesyndication"],
iframe[src*="exoclick"], iframe[src*="juicyads"],
iframe[src*="trafficjunky"], iframe[src*="popads"],
iframe[id^="aswift"], ins.adsbygoogle,
a[href*="/click?"][target="_blank"] {
  display: none !important; visibility: hidden !important;
  height: 0 !important; width: 0 !important;
}
"""

_JS = r"""
(function () {
  if (window.__M3U8DL_ADB) { return; }
  window.__M3U8DL_ADB = { css: __CSS__, removed: 0 };

  function injectCss() {
    try {
      if (document.getElementById("__m3u8dl_adb_style")) { return; }
      var s = document.createElement("style");
      s.id = "__m3u8dl_adb_style";
      s.textContent = __CSS__;
      (document.head || document.documentElement).appendChild(s);
    } catch (e) {}
  }

  // 动态插入的广告 iframe（弹窗广告多为运行时创建）
  var HOSTS = __HOSTS__;
  function sweep() {
    try {
      var fs = document.querySelectorAll("iframe[src]");
      for (var i = 0; i < fs.length; i++) {
        var f = fs[i], src = String(f.src || "");
        if (!src) { continue; }
        for (var j = 0; j < HOSTS.length; j++) {
          if (src.indexOf(HOSTS[j]) >= 0) {
            f.style.display = "none";
            try { f.src = "about:blank"; } catch (e) {}
            window.__M3U8DL_ADB.removed++;
            break;
          }
        }
      }
    } catch (e) {}
  }

  injectCss();
  try {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () {
        injectCss(); sweep();
      });
    } else { sweep(); }
    var mo = new MutationObserver(function () { injectCss(); sweep(); });
    mo.observe(document.documentElement || document,
               { childList: true, subtree: true });
    setInterval(sweep, 3000);
  } catch (e) {}
})();
"""


def build_script() -> str:
    """生成元素层注入脚本（CSS + 动态清理）。"""
    import json

    hosts = [d for d in AD_HOSTS if not d.startswith(("google-analytics",
                                                      "googletagmanager",
                                                      "hotjar", "clarity",
                                                      "sentry", "quantserve",
                                                      "scorecardresearch",
                                                      "moatads", "doubleverify"))]
    return (_JS.replace("__CSS__", json.dumps(_CSS))
            .replace("__HOSTS__", json.dumps(hosts)))
