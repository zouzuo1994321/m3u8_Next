"""媒体引擎自检: 用内置 ffmpeg 生成一段 H.264 HLS, 交给 QMediaPlayer 解码。

用途: 验证打包后的 exe 里 QtMultimedia 的 FFmpeg 插件与解码库是否可用
(预览功能依赖此链路)。通过 `M3U8视频下载器.exe --selftest-media` 触发。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from .diag import log as _diag
from .fftools import find_ffmpeg, run_hidden


def _make_hls(dirpath: Path, ffmpeg: str) -> Path:
    m3u8 = dirpath / "st.m3u8"
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x180:rate=20:duration=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
        "-hls_segment_filename", str(dirpath / "s%03d.ts"),
        str(m3u8),
    ]
    run_hidden(cmd, check=True, capture_output=True, timeout=120)
    return m3u8


def run_media_selftest(timeout_ms: int = 9000) -> bool:
    """返回是否成功解码出视频帧。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        _diag("media_selftest: ffmpeg 未找到")
        return False

    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

    tmp = Path(tempfile.mkdtemp(prefix="m3u8dl_st_"))
    try:
        m3u8 = _make_hls(tmp, ffmpeg)
    except Exception as e:
        _diag(f"media_selftest: 生成测试流失败 {e}")
        return False

    player = QMediaPlayer()
    sink = QVideoSink()
    player.setVideoSink(sink)
    stat = {"frames": 0, "err": ""}
    sink.videoFrameChanged.connect(
        lambda f: stat.__setitem__("frames", stat["frames"] + 1)
        if f.isValid() else None)
    player.errorOccurred.connect(
        lambda e, s: stat.__setitem__("err", s or str(e)))

    loop = QEventLoop()
    player.setSource(QUrl.fromLocalFile(str(m3u8)))
    player.play()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    try:
        player.stop()
    except Exception:
        pass

    ok = stat["frames"] > 0
    _diag(f"media_selftest: frames={stat['frames']} ok={ok} "
          f"err={stat['err'] or '(none)'}")
    return ok


def run_adv_selftest() -> bool:
    """进阶能力自检(打包验证用): 深度搜索 / 缓存捕捉 / 更多功能 / 页面数据桥。

    重点验证两件在打包后容易丢的东西:
    1. ``QtWebChannel`` 的 ``:/qtwebchannel/qwebchannel.js`` 资源是否被收进 exe
       (读取失败则页面拿不到 ``__M3U8DL_CH``, 缓存捕捉全废);
    2. 各模块的注入脚本能否正常生成(纯 Python 侧, 不依赖浏览器)。
    """
    checks = []

    # 1. 页面数据桥: 必须能读到 Qt 内置的 qwebchannel.js
    try:
        from .bridge import qwebchannel_js

        js = qwebchannel_js()
        checks.append(("bridge.qwebchannel.js", len(js) > 2000, f"{len(js)} 字节"))
    except Exception as e:
        checks.append(("bridge.qwebchannel.js", False, repr(e)))

    # 2. 深度搜索脚本
    try:
        from . import deepsearch

        src = deepsearch.build_script()
        ok = (len(src) > 5000 and "__M3U8DL_DEEP" in src
              and "__SELF_SRC__" not in src)
        checks.append(("deepsearch.build_script", ok, f"{len(src)} 字节"))
        res = deepsearch.parse_result('{"urls":[]}')
        checks.append(("deepsearch.parse_result", isinstance(res, dict), str(res)))
    except Exception as e:
        checks.append(("deepsearch", False, repr(e)))

    # 3. 缓存捕捉脚本
    try:
        from . import capture

        src = capture.build_script("42")
        ok = (len(src) > 1000 and "appendBuffer" in src
              and "__B64__" not in src and "__SID__" not in src)
        checks.append(("capture.build_script", ok, f"{len(src)} 字节"))
        checks.append(("capture.is_ts",
                       capture.is_ts(b"\x47" + b"\x00" * 187 + b"\x47"),
                       "TS 同步字节识别"))
    except Exception as e:
        checks.append(("capture", False, repr(e)))

    # 4. 更多功能: 图片伪装剥离
    try:
        from . import fragtools

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16 + b"IEND" + b"\xaeB`\x82"
        payload = b"\x47" + b"\x11" * 187
        out = fragtools.strip_image_wrapper(png + payload)
        checks.append(("fragtools.strip_image_wrapper", out == payload,
                       f"{len(png) + len(payload)} -> {len(out)} 字节"))
    except Exception as e:
        checks.append(("fragtools", False, repr(e)))

    # 5. 录制与自动化(猫抓按钮条的 6 个按钮)
    try:
        import tempfile

        from . import recorder
        from .recorder import RecordStore

        src = recorder.build_script("7")
        need = ("__M3U8DL_REC_START", "RTCPeerConnection", "getDisplayMedia",
                "captureStream", "MediaRecorder", "__M3U8DL_REC_MIMES")
        missing = [n for n in need if n not in src]
        checks.append(("recorder.build_script", not missing,
                       f"{len(src)} 字节" + (f" 缺 {missing}" if missing else "")))

        js = recorder.start_js("webrtc", 1, "video/webm;codecs=vp9,opus")
        checks.append(("recorder.start_js", '"kind": "webrtc"' in js,
                       js[:70]))

        miss_hint = [k for k, v in recorder.START_HINTS.items()
                     if not isinstance(v, str) or not v]
        checks.append(("recorder.START_HINTS", not miss_hint,
                       f"{len(recorder.START_HINTS)} 种结果"))
        checks.append(("recorder.ext_for_mime",
                       recorder.ext_for_mime("video/webm;codecs=vp9") == "webm"
                       and recorder.ext_for_mime("video/mp4") == "mp4",
                       "webm / mp4"))

        # 落盘要能跨盘(临时目录常在 C:, 输出常在其他盘)
        work = Path(tempfile.mkdtemp(prefix="m3u8dl_st_"))
        store = RecordStore(work)
        store.begin("element")
        store.add(b"\x1a\x45\xdf\xa3" + b"\x00" * 200)
        out = store.finish(work / "out", "st_rec", "video/webm")
        ok = out is not None and out.exists() and out.suffix == ".webm"
        checks.append(("recorder.RecordStore.finish", ok,
                       f"{out.name if out else 'None'} "
                       f"{out.stat().st_size if out else 0} 字节"))
    except Exception as e:
        checks.append(("recorder", False, repr(e)))

    # 6. 请求头策略 / 广告拦截(打包验证用)
    try:
        from . import adblock, reqhdr

        vs = reqhdr.build_variants(
            "https://cdn.example.com/a/master.m3u8?t=1",
            {"Referer": "https://page.example.com/x"},
            "https://page.example.com/x")
        names = [n for n, _ in vs]
        checks.append(("reqhdr.build_variants",
                       names[:3] == ["captured", "site_root", "no_referer"],
                       " → ".join(names)))
        # 噪声头会触发"头集合指纹"校验(实测 Accept-Language 即 403), 精简档
        # 必须能把它剥掉 —— 这是 v1.1.10 的核心修复点
        mini = reqhdr.minimal_headers({
            "User-Agent": "UA", "Referer": "R", "Origin": "O",
            "Accept-Language": "zh-CN,zh;q=0.9", "Accept": "*/*",
            "sec-ch-ua": "x"})
        checks.append(("reqhdr.minimal_headers",
                       set(mini) == {"User-Agent", "Referer", "Origin"},
                       f"剥噪声头 -> {sorted(mini)}"))
        checks.append(("adblock.should_block",
                       adblock.should_block("https://s.popads.net/p.js")
                       and not adblock.should_block(
                           "https://cdn.x/a/master.m3u8"),
                       "拦广告 / 放行 m3u8"))
        src = adblock.build_script()
        checks.append(("adblock.build_script",
                       len(src) > 800 and "__CSS__" not in src,
                       f"{len(src)} 字节"))
    except Exception as e:
        checks.append(("adblock/reqhdr", False, repr(e)))

    # 7. mp4 直链 + 系统浏览器嗅探(v1.1.11)
    try:
        import struct

        from . import cdp, mp4info

        # 造一个最小 MP4(ftyp + moov{mvhd,trak{tkhd}}), 断言解析结果。
        # 时长 496s / 854x480 正是实测那条 mxcontent 直链的真实数值。
        def _box(typ: bytes, payload: bytes) -> bytes:
            return struct.pack(">I", 8 + len(payload)) + typ + payload

        mvhd = (bytes(4) + struct.pack(">II", 0, 0)
                + struct.pack(">II", 1000, 496000) + bytes(80))
        tkhd = bytes(84) + struct.pack(">II", 854 << 16, 480 << 16)
        blob = (_box(b"ftyp", b"isom" + bytes(4) + b"isomiso2")
                + _box(b"moov", _box(b"mvhd", mvhd)
                       + _box(b"trak", _box(b"tkhd", tkhd))))
        got = mp4info.parse_bytes(blob, total=190_530_000)
        ok = (int(got.get("duration", 0)) == 496
              and got.get("width") == 854 and got.get("height") == 480
              and got.get("bitrate_kbps", 0) > 3000)
        checks.append(("mp4info.parse_bytes", ok, str(got)))

        name, exe = cdp.find_browser()
        checks.append(("cdp.find_browser", bool(name) == bool(exe),
                       f"{name or '未安装'}"))
        checks.append(("cdp 视频过滤",
                       bool(cdp._VIDEO_RE.search("https://x/a/master.m3u8"))
                       and bool(cdp._VIDEO_RE.search("https://x/v.mp4?s=1"))
                       and not cdp._SKIP_RE.search("https://x/v.mp4?s=1")
                       and bool(cdp._SKIP_RE.search("https://x/s-1.ts")),
                       "m3u8/mp4 命中, ts 放过"))
        checks.append(("cdp._POKE_JS", "play()" in cdp._POKE_JS
                       and "muted" in cdp._POKE_JS,
                       f"{len(cdp._POKE_JS)} 字节"))
    except Exception as e:
        checks.append(("cdp/mp4info", False, repr(e)))

    try:
        from . import adlist

        n = len(adlist.AD_DOMAINS)
        checks.append(("adlist.AD_DOMAINS", n > 40000, f"{n} 个域名"))
    except Exception as e:
        checks.append(("adlist", False, repr(e)))

    # 8. B站支持(v1.1.14): 纯本地判定, 不联网
    try:
        from . import bili
        from .downloader import M3U8Downloader

        checks.append(("bili.is_bili_url",
                       bili.is_bili_url("https://www.bilibili.com/video/BV1xx")
                       and bili.is_bili_url("https://b23.tv/abc")
                       and not bili.is_bili_url("https://x.com/v/BV1xx"),
                       "BV / 短链命中, 其它站点放过"))
        tgt = bili.parse_target(
            "https://www.bilibili.com/video/BV1xx411c7mD/?p=3")
        checks.append(("bili.parse_target", tgt == ("bvid", "BV1xx411c7mD", 3), str(tgt)))
        st = bili.build_streams(
            {"video": [{"id": 80, "width": 1920, "height": 1080,
                        "codecs": "avc1.640028", "bandwidth": 3000000,
                        "baseUrl": "https://v/1.m4s"}],
             "audio": [{"id": 30280, "bandwidth": 190000,
                        "baseUrl": "https://a/1.m4s"}]}, 300)
        checks.append(("bili.build_streams",
                       len(st) == 1 and st[0].audio_url == "https://a/1.m4s"
                       and st[0].resolution == "1920x1080"
                       and st[0].size_bytes > 0,
                       st[0].label if st else "空"))
        cmd = M3U8Downloader.dash_merge_cmd("ffmpeg.exe", "v.part", "a.part",
                                            "out.mp4")
        checks.append(("downloader.dash_merge_cmd",
                       cmd.count("-i") == 2 and "copy" in cmd
                       and "+faststart" in cmd, " ".join(cmd[-6:])))
    except Exception as e:
        checks.append(("bili", False, repr(e)))

    all_ok = True
    for name, ok, info in checks:
        _diag(f"adv_selftest: {name} = {'OK' if ok else 'FAIL'} ({info})")
        all_ok = all_ok and ok
    _diag(f"adv_selftest: total={len(checks)} ok={all_ok}")
    return all_ok


#: 录制自检用的页面: 一块持续重绘的 canvas(不依赖任何解码器)
_REC_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:#000">
<canvas id="c" width="320" height="180"></canvas>
<script>
var cv = document.getElementById('c'), ctx = cv.getContext('2d'), n = 0;
function draw() {
  n++;
  ctx.fillStyle = 'hsl(' + (n * 9 % 360) + ',70%,50%)';
  ctx.fillRect(0, 0, 320, 180);
  ctx.fillStyle = '#fff';
  ctx.font = '28px sans-serif';
  ctx.fillText('frame ' + n, 20, 100);
  requestAnimationFrame(draw);
}
draw();
</script></body></html>
"""


def run_record_selftest(total_ms: int = 22000) -> bool:
    """录制链路自检（打包验证用）。

    在打包后的 exe 里走一遍完整链路：``canvas.captureStream`` → ``MediaRecorder``
    → 分片 base64 经 **QWebChannel** 回传 → ``RecordStore`` 落盘 → 校验 EBML 头。

    这一步能抓出纯 Python 测试抓不到的打包问题：``qwebchannel.js`` 资源缺失、
    QtWebEngineProcess 未随包、录制编码在打包环境中不可用等。
    ``runJavaScript`` 无法把 JS 对象转回 Python（得到空串），故状态一律
    ``JSON.stringify`` 后解析。
    """
    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtWebEngineWidgets import QWebEngineView

    from . import recorder
    from .bridge import PageBridge
    from .recorder import RecordStore

    work = Path(tempfile.mkdtemp(prefix="m3u8dl_rec_"))
    store = RecordStore(work)
    view = QWebEngineView()
    view.resize(380, 260)
    view.show()

    page = view.page()
    bridge = PageBridge(lambda *a: None, lambda *a: None,
                        lambda sid, kind, data: store.add(data))
    if not bridge.install(page):
        _diag("record_selftest: QWebChannel 安装失败")
        return False

    profile = page.profile()
    for name, src in (("m3u8dl-bridge", bridge.inject_script()),
                      (recorder.SCRIPT_NAME, recorder.build_script("st"))):
        if not _install_profile_script(profile, name, src):
            _diag(f"record_selftest: 注入脚本失败 {name}")
            return False

    st = {"phase": "load", "start": "", "mimes": 0, "ok": False, "info": ""}
    loop = QEventLoop()

    def finish(ok: bool, info: str):
        st["ok"], st["info"] = ok, info
        try:
            view.close()
        except Exception:
            pass
        loop.quit()

    def phase_check():
        """落盘并校验。"""
        st["phase"] = "check"
        size, chunks = store.bytes, store.chunks
        out = store.finish(work / "out", "selftest_rec", "video/webm")
        if out is None:
            finish(False, f"落盘失败(收到 {chunks} 片 / {size} 字节)")
            return
        head = out.read_bytes()[:4]
        finish(head == b"\x1a\x45\xdf\xa3" and size > 1000,
               f"{chunks} 片 / {size} 字节 / 头={head.hex()} / "
               f"编码 {st['mimes']} 种 / 启动={st['start']!r}")

    def phase_stop():
        st["phase"] = "stop"
        try:
            page.runJavaScript(recorder.STOP_JS)
        except Exception:
            pass
        QTimer.singleShot(1500, phase_check)

    def phase_start():
        st["phase"] = "record"
        js = ("(function(){var R=window.__M3U8DL_REC;"
              "if(!R){return 'no-rec';}"
              "var c=document.getElementById('c');"
              "if(!c){return 'no-canvas';}"
              "var s=c.captureStream(25);"
              "R.tracks.video.length=0;R.tracks.audio.length=0;"
              "R.tracks.video.push(s.getVideoTracks()[0]);"
              "return window.__M3U8DL_REC_START("
              "{kind:'webrtc',videoBits:800000});})()")

        def on_started(res):
            st["start"] = "" if res is None else str(res)
            if st["start"] != "started":
                st["phase"] = "mimes"
                page.runJavaScript(
                    recorder.MIMES_JS,
                    lambda raw: finish(False, "录制未启动: "
                                       f"{st['start'] or '无响应'}"))
                return
            QTimer.singleShot(3500, phase_stop)

        page.runJavaScript(recorder.MIMES_JS, 0,
                           lambda raw: st.__setitem__(
                               "mimes", len(recorder.parse_mimes(raw))))
        page.runJavaScript(js, 0, on_started)

    def on_loaded(ok: bool):
        if not ok:
            finish(False, "页面加载失败")
            return
        # 等注入脚本 + 首帧绘制
        QTimer.singleShot(900, phase_start)

    page.loadFinished.connect(on_loaded)
    page.setHtml(_REC_PAGE, QUrl("http://m3u8dl.local/selftest"))
    store.begin("element")

    QTimer.singleShot(total_ms, lambda: finish(False, "超时"))
    loop.exec()

    _diag(f"record_selftest: phase={st['phase']} ok={st['ok']}")
    _diag(f"record_selftest: {st['info']}")
    return bool(st["ok"])


def _install_profile_script(profile, name: str, source: str) -> bool:
    """往 profile 挂一个 DocumentCreation 脚本（同名先移除）。"""
    from PySide6.QtWebEngineCore import QWebEngineScript

    try:
        script = QWebEngineScript()
        script.setName(name)
        script.setSourceCode(source)
        script.setInjectionPoint(
            QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        scripts = profile.scripts()
        for old in list(scripts.toList()):
            if old.name() == name:
                scripts.remove(old)
        scripts.insert(script)
        return True
    except Exception:
        return False
