"""入口: 进程清理 -> Qt 应用 -> 主窗口 -> 退出时强制终止自身进程。

启动时: 清理之前未关闭的同名 exe 残留进程(单实例)。
关闭后: os._exit 强制终止自身进程(含 QtWebEngine 子进程与残留线程)。
"""
import os
import re
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m3u8dl.procguard import cleanup_stale_instances, force_exit, terminate_children


def _crash_log(msg: str):
    """诊断日志: 写入 %TEMP%/m3u8dl_crash.log (frozen 环境排查用)"""
    try:
        p = Path(os.environ.get("TEMP", ".")) / "m3u8dl_crash.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _excepthook(exc_type, exc_value, exc_tb):
    """全局异常钩子: 记录未捕获异常到崩溃日志"""
    _crash_log("=== UNCAUGHT EXCEPTION ===")
    _crash_log("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    _crash_log("=== END ===")
    try:
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    except Exception:
        # windowed 模式下 stderr 为 None, 默认钩子可能再次抛错, 忽略即可
        pass


def _safe_print(msg: str):
    """安全输出: windowed(frozen) 模式下 sys.stdout 可能为 None, 直接 print 会抛
    AttributeError 导致启动崩溃, 故判空后打印, 同时写入诊断日志。"""
    _crash_log(msg)
    try:
        if sys.stdout is not None:
            print(msg)
    except Exception:
        pass


def _set_app_id():
    """告诉 Windows 这个进程属于自己的 AppID。

    不设的话任务栏会把窗口归到 python.exe / 通用图标下, 即使设置了
    setWindowIcon 也只在小图标上生效, 大图标/跳转列表里仍是"无名进程"。
    必须在任何窗口创建之前调用。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "SiYueAperture.M3U8VideoDownloader")
    except Exception:
        pass


def main():
    sys.excepthook = _excepthook
    _crash_log(f"--- app start (frozen={getattr(sys, 'frozen', False)}) ---")
    _set_app_id()

    # 1. 启动时清理之前未关闭的残留进程
    logs = []
    killed = cleanup_stale_instances(log=lambda m: logs.append(m))
    for m in logs:
        _safe_print(m)
    if killed:
        _safe_print(f"已清理 {killed} 个残留进程")
    _crash_log(f"cleanup done, killed={killed}")

    # 2. 设置 QtWebEngine / QtMultimedia 参数(必须在 QApplication 创建前设置)
    #    --autoplay-policy=no-user-gesture-required: 允许视频自动播放,
    #    部分站点播放器需要自动播放才能触发视频流请求(否则嗅探拦截不到地址)
    os.environ.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "--autoplay-policy=no-user-gesture-required",
    )
    # 预览用 QtMultimedia 的 FFmpeg 后端: 自带 H.264/AAC 解码器,
    # 而内置 QtWebEngine(开源编译版)不含这些专有编解码器, 无法播放直播流
    os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from m3u8dl import version
    from m3u8dl.ui.main_window import MainWindow, assets_dir
    from m3u8dl.ui.theme import DARK_QSS

    app = QApplication(sys.argv)
    app.setApplicationName("M3U8视频下载器")
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)

    # 应用级图标: 所有对话框(关于/警告/文件框)与任务栏都用它, 而不是 Qt 默认图标
    _assets = assets_dir()
    for _ic in ("icon.ico", "icon.png"):
        _p = _assets / _ic
        if _p.exists():
            app.setWindowIcon(QIcon(str(_p)))
            _crash_log(f"app icon: {_p}")
            break

    # 3. 设置 Chrome 风格 User-Agent
    #    QtWebEngine 默认 UA 含 "QtWebEngine/6.11.2" 标识, 部分站点(如 Stripchat
    #    类直播站)会检测到嵌入式浏览器身份并拒绝提供视频流, 导致页面自带播放器
    #    报 "This video file cannot be played. (Error Code: 102630)"。
    #    移除该标识后伪装成普通 Chrome, 站点正常提供视频流, 嗅探也能捕获到地址。
    try:
        from PySide6.QtWebEngineCore import QWebEngineProfile
        _profile = QWebEngineProfile.defaultProfile()
        _ua = _profile.httpUserAgent()
        _chrome_ua = re.sub(r"QtWebEngine/[\d.]+\s*", "", _ua)
        if _chrome_ua != _ua:
            _profile.setHttpUserAgent(_chrome_ua)
            _safe_print(f"User-Agent 已设置为 Chrome 风格(移除 QtWebEngine 标识)")
            _crash_log(f"UA: {_ua} -> {_chrome_ua}")
    except Exception:
        _crash_log("setHttpUserAgent FAILED:\n" + traceback.format_exc())

    # 媒体引擎自检(打包验证用): 生成一段 H.264 HLS 交给 QMediaPlayer 解码
    if "--selftest-media" in sys.argv:
        try:
            from m3u8dl.selftest import run_media_selftest
            ok = run_media_selftest()
            _crash_log(f"SELFTEST_MEDIA={'OK' if ok else 'FAIL'}")
        except Exception:
            _crash_log("SELFTEST_MEDIA=ERROR:\n" + traceback.format_exc())
        force_exit(0)

    # 进阶能力自检(打包验证用): 数据桥资源 + 三个功能的注入脚本/工具集
    if "--selftest-adv" in sys.argv:
        try:
            from m3u8dl.selftest import run_adv_selftest
            ok = run_adv_selftest()
            _crash_log(f"SELFTEST_ADV={'OK' if ok else 'FAIL'}")
        except Exception:
            _crash_log("SELFTEST_ADV=ERROR:\n" + traceback.format_exc())
        force_exit(0)

    # 录制链路自检(打包验证用): canvas -> MediaRecorder -> QWebChannel -> 落盘
    if "--selftest-rec" in sys.argv:
        try:
            from m3u8dl.selftest import run_record_selftest
            ok = run_record_selftest()
            _crash_log(f"SELFTEST_REC={'OK' if ok else 'FAIL'}")
        except Exception:
            _crash_log("SELFTEST_REC=ERROR:\n" + traceback.format_exc())
        force_exit(0)

    # 4. 启动动效: 图标卡片先出现, 播完再淡入主窗口
    #    顺序很重要 —— 卡片要**先** show, 用户立刻有反馈; 主窗口在卡片播放
    #    期间同步构建(构造过程会拉起预览服务/WebView, 需要几百毫秒)。
    from m3u8dl.ui.splash import build_splash, reveal_window

    splash = build_splash(
        _assets, version.APP_NAME,
        f"{version.VERSION} · Build {version.BUILD}")
    win = None

    def _reveal():
        # 卡片淡出的最后一段触发: 主窗口从 0 淡入, 两段动画首尾相接
        if win is not None:
            reveal_window(win)
            _crash_log("splash done, window revealed")

    if splash is not None:
        splash.play(_reveal)
        app.processEvents()          # 先把卡片画出来, 再去做耗时的构造
        _crash_log("splash shown")
    win = MainWindow()
    if splash is None:
        win.show()
    _crash_log("window constructed")

    # 退出路径诊断: 区分「自身关闭」与「外部 WM_CLOSE(任务管理器/关机)」
    # closeEvent 里另有 spontaneous 标记, 两者对照即可定位莫名退出的原因
    app.aboutToQuit.connect(lambda: _crash_log("SIGNAL aboutToQuit"))
    app.lastWindowClosed.connect(lambda: _crash_log("SIGNAL lastWindowClosed"))

    rc = app.exec()
    _crash_log(f"app.exec() returned rc={rc!r}")

    # 2. 关闭后: 先终止子进程(QtWebEngineProcess/ffmpeg 等), 再关掉自身进程
    try:
        n = terminate_children()
        _crash_log(f"terminate_children killed={n}")
    except Exception:
        _crash_log("terminate_children FAILED:\n" + traceback.format_exc())
    _crash_log("calling force_exit")
    _crash_log(
        f"streams: stdout={sys.stdout!r} stderr={sys.stderr!r} "
        f"stdout_closed={getattr(sys.stdout, 'closed', 'n/a')} "
        f"stderr_closed={getattr(sys.stderr, 'closed', 'n/a')}"
    )
    force_exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
