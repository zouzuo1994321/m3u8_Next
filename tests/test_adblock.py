"""内置广告拦截（adblock）验证。

两层：
1. 网络层 —— ``VideoUrlInterceptor`` 命中广告规则时 ``info.block(True)``，
   请求不出网；但**视频地址与主框架导航永不拦截**（拦错就把候选/页面搞坏）。
2. 元素层 —— 注入 CSS 隐藏广告位 + MutationObserver 清理动态插入的广告 iframe。

同时验证开关：关掉后拦截器不再 block，元素脚本被移除。

用法: QT_QPA_PLATFORM=offscreen python tests/test_adblock.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from m3u8dl import adblock  # noqa: E402

PASS, FAIL = [], []


def check(ok, msg):
    (PASS if ok else FAIL).append(msg)
    print(f"[{'PASS' if ok else 'FAIL'}] {msg}")


# ---------------------------------------------------------------- 规则层
def test_rules():
    blocks = [
        ("https://ad.doubleclick.net/ddm/adj/123.js", "google 广告"),
        ("https://s.popads.net/pop.js", "popads 弹窗"),
        ("https://exoclick.com/ad.php", "exoclick"),
        ("https://a.trafficjunky.net/vast.xml", "trafficjunky"),
        ("https://cdn.site.com/adserver/banner.gif", "adserver 路径"),
        ("https://x.com/pb.js?ad_zone=12", "ad_zone 参数"),
        ("https://www.googletagmanager.com/gtag/js?id=G-1", "统计脚本"),
    ]
    for url, why in blocks:
        check(adblock.should_block(url), f"拦截 {why}: {url[:52]}")

    keeps = [
        ("https://strm13.uqload.vc/hls2/03/x/master.m3u8?t=abc", "m3u8 清单"),
        ("https://cdn.x/a/mid_1280x720_2500.mp4", "mp4 直链"),
        ("https://cdn.x/a/seg1.ts", "分片(不在广告域)"),
        ("https://livecamrips.to/video/16402068", "页面接口"),
        ("https://site.com/api/list.json", "普通接口"),
    ]
    for url, why in keeps:
        check(not adblock.should_block(url), f"放行 {why}: {url[:52]}")

    check(not adblock.should_block("https://ad.doubleclick.net/x.js",
                                   "ResourceTypeMainFrame"),
          "主框架导航不拦截(否则页面打不开)")
    check(adblock.should_block("https://ad.doubleclick.net/x.js",
                               "ResourceTypeScript"),
          "脚本类广告资源拦截")


def test_script():
    src = adblock.build_script()
    check(len(src) > 800, f"元素层脚本已生成({len(src)} 字节)")
    check("__CSS__" not in src and "__HOSTS__" not in src, "占位符已替换")
    check("MutationObserver" in src, "含动态清理(MutationObserver)")
    check("display: none" in src, "含广告位隐藏规则")
    check('"exoclick.com"' in src or "exoclick.com" in src,
          "iframe 清理域名表含 exoclick")


# ---------------------------------------------------------------- 拦截器层
class FakeInfo:
    """模拟 QWebEngineUrlRequestInfo（只实现用到的接口）。"""

    def __init__(self, url, rtype_name="ResourceTypeImage"):
        self._url = url
        self._rtype = rtype_name
        self.blocked = False

    def requestUrl(self):
        from PySide6.QtCore import QUrl

        return QUrl(self._url)

    def resourceType(self):
        from PySide6.QtWebEngineCore import QWebEngineUrlRequestInfo as RT

        return getattr(RT.ResourceType, self._rtype)

    def httpHeaders(self):
        return {}

    def firstPartyUrl(self):
        from PySide6.QtCore import QUrl

        return QUrl("https://livecamrips.to/video/1")

    def block(self, yes):
        self.blocked = bool(yes)


def test_interceptor():
    from PySide6.QtWidgets import QApplication

    from m3u8dl.websniff import VideoUrlInterceptor

    app = QApplication.instance() or QApplication(sys.argv)
    ic = VideoUrlInterceptor()
    check(ic.adblock is True, "拦截器默认开启广告拦截")

    ad = FakeInfo("https://s.popads.net/pop.js", "ResourceTypeScript")
    ic.interceptRequest(ad)
    check(ad.blocked, "广告脚本被 block")

    ad2 = FakeInfo("https://exoclick.com/banner.jpg", "ResourceTypeImage")
    ic.interceptRequest(ad2)
    check(ad2.blocked, "广告图片被 block")

    vid = FakeInfo("https://strm13.uqload.vc/hls2/a/master.m3u8?t=1",
                   "ResourceTypeXhr")
    ic.active = True
    ic.interceptRequest(vid)
    check(not vid.blocked, "视频清单不被 block")
    check(any(".m3u8" in u for u in ic.take_urls()),
          "视频清单仍被正常收集")

    mp4 = FakeInfo("https://vidwestxyz.com/452561820/24013984/mid.mp4",
                   "ResourceTypeMedia")
    ic.interceptRequest(mp4)
    check(not mp4.blocked, "贴片 mp4 不被 block(留给用户判断)")

    nav = FakeInfo("https://livecamrips.to/video/16402068",
                   "ResourceTypeMainFrame")
    ic.interceptRequest(nav)
    check(not nav.blocked, "主框架导航不被 block")

    # 关闭后不再拦截
    ic.adblock = False
    ad3 = FakeInfo("https://s.popads.net/pop2.js", "ResourceTypeScript")
    ic.interceptRequest(ad3)
    check(not ad3.blocked, "开关关闭后不再 block")
    app.processEvents()


def test_toggle():
    """主窗口开关: 关掉后拦截器同步关闭、元素脚本被移除。"""
    from PySide6.QtWidgets import QApplication

    from m3u8dl.ui.main_window import MainWindow, WEBENGINE_OK

    if not WEBENGINE_OK:
        check(False, "当前环境无 QtWebEngine, 跳过开关用例")
        return
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow()
    try:
        ok, msg = win._toggle_adblock(False)
        check(ok and win._url_interceptor.adblock is False,
              f"关闭广告拦截: {msg}")
        names = [s.name() for s in win._profile.scripts().toList()]
        check(adblock.SCRIPT_NAME not in names, "元素脚本已移除")

        ok2, msg2 = win._toggle_adblock(True)
        check(ok2 and win._url_interceptor.adblock is True,
              f"重新开启: {msg2}")
        names = [s.name() for s in win._profile.scripts().toList()]
        check(adblock.SCRIPT_NAME in names, "元素脚本已重新挂上")
    finally:
        win.close()
        win.deleteLater()
        app.processEvents()


def main():
    test_rules()
    test_script()
    test_interceptor()
    test_toggle()

    print()
    if FAIL:
        print(f"ADBLOCK TEST FAILED: {len(FAIL)} 项失败")
        for f in FAIL:
            print("  -", f)
        return 1
    print(f"ADBLOCK TEST OK: {len(PASS)} 项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
