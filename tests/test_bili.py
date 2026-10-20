"""B站支持回归测试(全离线, 不碰网络)。

覆盖:
- 地址识别 / BV·av·分P 解析
- 风控 Cookie bootstrapping 是否真的调到 spi 接口
- view + playurl 解析: 画质排序 / 编码择优 / 音轨配对 / 体积估算
- durl 兜底(番剧/老接口, 音轨内置)
- 412 / 非 JSON / code!=0 的可读错误
- 下载器 DASH 分流: audio_url 存在时走 _run_dash、合并命令正确
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3u8dl import bili                      # noqa: E402
from m3u8dl.downloader import DownloadConfig, M3U8Downloader  # noqa: E402

ok = 0


def chk(cond, msg=""):
    global ok
    if not cond:
        raise AssertionError(msg)
    ok += 1


# ------------------------------------------------------------------ 夹具
VIEW = {
    "code": 0, "data": {
        "bvid": "BV1xx411c7mD", "aid": 1234, "title": "测试稿件",
        "duration": 300,
        "pages": [
            {"cid": 111, "page": 1, "part": "第一话", "duration": 300},
            {"cid": 222, "page": 2, "part": "第二话", "duration": 240},
        ],
    },
}

PLAYURL = {
    "code": 0, "data": {
        "quality": 80, "accept_quality": [80, 64, 32],
        "dash": {
            "duration": 300,
            "video": [
                {"id": 80, "width": 1920, "height": 1080, "codecs": "avc1.640028",
                 "bandwidth": 3000000, "baseUrl": "https://v.example/1080-avc.m4s"},
                {"id": 80, "width": 1920, "height": 1080, "codecs": "av01.0.08M.08",
                 "bandwidth": 2200000, "baseUrl": "https://v.example/1080-av1.m4s"},
                {"id": 64, "width": 1280, "height": 720, "codecs": "hev1.1.6.L120",
                 "bandwidth": 1500000, "base_url": "https://v.example/720-hevc.m4s"},
                {"id": 32, "width": 852, "height": 480, "codecs": "avc1.64001F",
                 "bandwidth": 700000, "baseUrl": "https://v.example/480.m4s"},
            ],
            "audio": [
                {"id": 30216, "bandwidth": 66000, "baseUrl": "https://a.example/64k.m4s"},
                {"id": 30280, "bandwidth": 190000, "baseUrl": "https://a.example/192k.m4s"},
                {"id": 30232, "bandwidth": 130000, "baseUrl": "https://a.example/132k.m4s"},
            ],
        },
    },
}

PLAYURL_DURL = {
    "code": 0, "data": {
        "quality": 32,
        "durl": [
            {"url": "https://v.example/a.mp4", "size": 10 * 1024 * 1024},
            {"url": "https://v.example/b.mp4", "size": 5 * 1024 * 1024},
        ],
    },
}


class Resp:
    def __init__(self, payload=None, status=200, ctype="application/json",
                 url="https://www.bilibili.com/video/BV1xx411c7mD"):
        self._payload = payload
        self.status_code = status
        self.headers = {"content-type": ctype}
        self.url = url

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """按参数路由的假会话, 记录调用过的接口。"""

    def __init__(self, view=None, playurl=None, spi=None):
        self.calls = []
        self.cookies = _Cookies()
        self.headers = {}
        self.trust_env = True
        self._view = view if view is not None else Resp(VIEW)
        self._playurl = playurl if playurl is not None else Resp(PLAYURL)
        self._spi = spi if spi is not None else Resp(
            {"code": 0, "data": {"b_3": "b3", "b_4": "b4"}})

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, dict(params or {})))
        if "finger/spi" in url:
            return self._spi
        if "web-interface/view" in url:
            self._view.params = params
            return self._view
        if "player/playurl" in url:
            self._playurl.params = params
            return self._playurl
        return Resp({"code": 0}, url=url)


class _Cookies:
    def __init__(self):
        self.jar = {}

    def set(self, k, v, domain=None):
        self.jar[k] = v

    def get(self, k, default=None):
        return self.jar.get(k, default)


def patch_session(fake):
    """让 bili.make_session 返回假会话, 但仍走真实的 Cookie 引导流程。"""
    def _mk(cookie="", use_proxy=True, timeout=15):
        bili._apply_cookie(fake, cookie)
        bili._bootstrap_cookies(fake, timeout)
        return fake
    bili.make_session = _mk


# ------------------------------------------------------- 1. 地址识别与解析
chk(bili.is_bili_url("https://www.bilibili.com/video/BV1xx411c7mD"), "应识别 B站 视频页")
chk(bili.is_bili_url("https://b23.tv/abc"), "应识别 b23 短链")
chk(bili.is_bili_url("https://m.bilibili.com/video/BV1xx411c7mD"), "应识别移动端域名")
chk(not bili.is_bili_url("https://example.com/video/BV1xx411c7mD"), "非 B站 域名不应识别")
chk(not bili.is_bili_url(""), "空串不应识别")
chk(not bili.is_bili_url("https://notbilibili.com/video/BV1"), "近似域名不应误判")

kind, val, page = bili.parse_target("https://www.bilibili.com/video/BV1xx411c7mD/?p=2")
chk((kind, val, page) == ("bvid", "BV1xx411c7mD", 2), f"BV+分P 解析错: {kind, val, page}")
kind, val, page = bili.parse_target("https://www.bilibili.com/video/av12345")
chk((kind, val, page) == ("aid", "12345", 1), "av 号解析错")
chk(bili.parse_target("https://b23.tv/abc") is None, "短链本身解析不出 BV(需跳转)")
chk(bili.parse_target("https://www.bilibili.com/read/cv999") is None, "专栏页不应解析成视频")

# ------------------------------------------------------- 2. 完整解析(假网络)
fake = FakeSession()
patch_session(fake)
res = bili.resolve("https://www.bilibili.com/video/BV1xx411c7mD")
urls = [c[0] for c in fake.calls]
chk(any("finger/spi" in u for u in urls), "必须先请求 finger/spi 拿 buvid")
chk(any("web-interface/view" in u for u in urls), "应请求 view 接口")
chk(any("player/playurl" in u for u in urls), "应请求 playurl 接口")
chk(fake.cookies.get("buvid3") == "b3", "应写入 buvid3")
chk(fake.cookies.get("buvid4") == "b4", "应写入 buvid4")
chk(bool(fake.cookies.get("_uuid")), "应补一个 _uuid")

chk(res.title == "测试稿件", f"标题取错: {res.title}")
chk(res.bvid == "BV1xx411c7mD", "bvid 应回填")
chk(res.cid == 111, f"默认应取 P1 的 cid, 实际 {res.cid}")
chk(res.duration_s == 300)
chk(len(res.streams) == 3, f"三个画质应产出三条流, 实际 {len(res.streams)}")
chk([s.qn for s in res.streams] == [80, 64, 32], "画质应按高到低排序")
chk(res.streams[0].resolution == "1920x1080")
chk(res.streams[0].codec.startswith("avc1"), "同画质应优先 avc1(兼容性最好)")
chk(res.streams[1].codec.startswith("hev1"), "720P 只有 hevc 时应选 hevc")
chk(res.streams[0].video_url == "https://v.example/1080-avc.m4s")
chk(all(s.audio_url == "https://a.example/192k.m4s" for s in res.streams),
    "音轨应取带宽最高那条")
chk(res.streams[0].audio_label == "190kbps", f"音轨标签错: {res.streams[0].audio_label}")
chk(res.streams[0].size_bytes > 0 and "MB" in res.streams[0].size_text,
    "应给出体积估算")
chk(res.streams[0].duration_text == "05:00", f"时长文案错: {res.streams[0].duration_text}")
chk(res.streams[0].headers.get("Referer", "").startswith("https://www.bilibili.com"),
    "CDN 请求头必须带 B站 Referer")
chk("80" in str(fake.calls[-1][1].get("qn")) or fake.calls[-1][1].get("qn") == 127,
    "playurl 应请求最高画质")
chk(fake.calls[-1][1].get("fnval") == 4048, "playurl 应要 DASH(fnval=4048)")
chk(res.cookie_tip == "", "本条有 1080P, 不该提示画质受限")
# 只有 480P/360P 时才提示(未登录的真实情况)
LOW = {"code": 0, "data": {"quality": 32, "dash": {
    "video": [{"id": 32, "width": 852, "height": 480, "codecs": "avc1.64001F",
               "bandwidth": 700000, "baseUrl": "https://v.example/480.m4s"}],
    "audio": [{"id": 30280, "bandwidth": 190000, "baseUrl": "https://a.example/a.m4s"}]}}}
patch_session(FakeSession(playurl=Resp(LOW)))
res_low = bili.resolve("https://www.bilibili.com/video/BV1xx411c7mD")
chk("未登录最高 480P" in res_low.cookie_tip, "只有 480P 时应提示画质受限")
chk(res.display_title.startswith("测试稿件") and "P1" in res.display_title,
    f"多P标题应标分P名: {res.display_title}")
ONE = {"code": 0, "data": {"bvid": "BV1xx411c7mD", "title": "单P稿件",
                           "duration": 60, "pages": [
                               {"cid": 1, "page": 1, "part": "正片",
                                "duration": 60}]}}
patch_session(FakeSession(view=Resp(ONE)))
res_one = bili.resolve("https://www.bilibili.com/video/BV1xx411c7mD")
chk(res_one.display_title == "单P稿件",
    f"单P标题不应带分P名: {res_one.display_title}")

# 多P: 标题带 P 名; p=2 取第二个 cid
fake2 = FakeSession()
patch_session(fake2)
res2 = bili.resolve("https://www.bilibili.com/video/BV1xx411c7mD?p=2")
chk(res2.cid == 222, f"p=2 应取第二个 cid, 实际 {res2.cid}")
chk(res2.page == 2 and "P2" in res2.display_title, f"多P标题应标 P 名: {res2.display_title}")

# ------------------------------------------------------- 3. durl 兜底
fake3 = FakeSession(playurl=Resp(PLAYURL_DURL))
patch_session(fake3)
res3 = bili.resolve("https://www.bilibili.com/video/BV1xx411c7mD")
chk(len(res3.streams) == 2, "durl 应产出两段")
chk(res3.streams[0].audio_url == "", "durl 自带音轨, 不应再配 audio_url")
chk(res3.streams[0].size_bytes == 10 * 1024 * 1024, "durl 体积应取接口给的 size")
chk("第1段" in res3.streams[0].label, f"多段应标注第几段: {res3.streams[0].label}")

# ------------------------------------------------------- 4. 错误映射
try:
    patch_session(FakeSession(view=Resp("被拦截", status=412, ctype="text/html")))
    bili.resolve("https://www.bilibili.com/video/BV1xx411c7mD")
    raise AssertionError("412 应抛错")
except bili.BiliError as e:
    chk("412" in str(e) and "风控" in str(e), f"412 提示不友好: {e}")

try:
    patch_session(FakeSession(view=Resp("<html>", ctype="text/html")))
    bili.resolve("https://www.bilibili.com/video/BV1xx411c7mD")
    raise AssertionError("非 JSON 应抛错")
except bili.BiliError as e:
    chk("非 JSON" in str(e), f"非 JSON 提示不友好: {e}")

try:
    patch_session(FakeSession(view=Resp({"code": -404, "message": "啥都木有"})))
    bili.resolve("https://www.bilibili.com/video/BV1xx411c7mD")
    raise AssertionError("code!=0 应抛错")
except bili.BiliError as e:
    chk("-404" in str(e) and "不存在" in str(e), f"code 提示不友好: {e}")

try:
    patch_session(FakeSession(playurl=Resp({"code": 0, "data": {}})))
    bili.resolve("https://www.bilibili.com/video/BV1xx411c7mD")
    raise AssertionError("没有可用流应抛错")
except bili.BiliError as e:
    chk("没有返回可下载" in str(e), f"空流提示不友好: {e}")

# ------------------------------------------------------- 5. 下载器 DASH 分流
cfg = DownloadConfig(url="https://v.example/a.m4s", save_dir=".", save_name="x",
                     audio_url="https://a.example/a.m4s")
chk(cfg.audio_url.endswith("a.m4s"), "DownloadConfig 应带 audio_url")

dispatched = {}


class Probe(M3U8Downloader):
    def _run_dash(self):
        dispatched["dash"] = True
        return "done"

    def _run_direct(self):
        dispatched["direct"] = True
        return "done"


dl = Probe(cfg, on_log=lambda m: None, on_progress=lambda d: None,
           on_state=lambda s: None, ffmpeg_path=None)
dl.run()
chk(dispatched.get("dash") is True and "direct" not in dispatched,
    "有 audio_url 时必须走 _run_dash")

cfg2 = DownloadConfig(url="https://v.example/a.mp4", save_dir=".", save_name="x")
d2 = {"direct": False}


class Probe2(M3U8Downloader):
    def _run_direct(self):
        d2["direct"] = True
        return "done"


Probe2(cfg2, on_log=lambda m: None, on_progress=lambda d: None,
       on_state=lambda s: None).run()
chk(d2["direct"], "没有 audio_url 的 mp4 仍应走 _run_direct")

cmd = M3U8Downloader.dash_merge_cmd("ffmpeg.exe", "v.part", "a.part", "out.mp4")
chk(cmd[0].endswith("ffmpeg.exe"), "合并命令应可执行 ffmpeg")
chk(["-i", "v.part"] == cmd[cmd.index("-i"):cmd.index("-i") + 2], "应把视频作为第 1 输入")
chk(cmd.count("-i") == 2, "应有视频/音频两个输入")
chk("-c" in cmd and cmd[cmd.index("-c") + 1] == "copy", "必须 -c copy 不重编码")
chk("+faststart" in cmd, "moov 应前移(faststart)")
chk(cmd[-1] == "out.mp4", "最后一项应是输出路径")

# ------------------------------------------------------- 6. Cookie 灌入
s_ = FakeSession()
bili._apply_cookie(s_, "SESSDATA=abc%3D; bili_jct=def; 坏数据")
chk(s_.cookies.get("SESSDATA") == "abc%3D", "SESSDATA 应写入")
chk(s_.cookies.get("bili_jct") == "def", "bili_jct 应写入")
bili._apply_cookie(s_, "")
chk(s_.cookies.get("SESSDATA") == "abc%3D", "空串不应清空已有 Cookie")

# 带 SESSDATA 时应判定为已登录、不给画质提示
fake4 = FakeSession()
fake4.cookies.set("SESSDATA", "x")
patch_session(fake4)
res4 = bili.resolve("https://www.bilibili.com/video/BV1xx411c7mD")
chk(res4.logged_in is True, "有 SESSDATA 应判定已登录")
chk(res4.cookie_tip == "", "已登录不该再提示画质受限")

print(f"BILI TEST OK: {ok} 项全部通过")
