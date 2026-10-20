"""主窗口: 网址嗅探 -> 结果列表 -> 实时预览 -> 参数填充 -> 静默下载。"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPen
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QSplitterHandle,
    QStackedLayout,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:  # QtMultimedia(FFmpeg 后端) 用于实时预览, 缺失时降级
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    MEDIA_OK = True
except ImportError:  # pragma: no cover
    MEDIA_OK = False

from .. import version
from .. import capture, deepsearch, fragtools, recorder
from .. import adblock
from ..bridge import PageBridge
from ..capture import CaptureStore, mux_with_ffmpeg
from ..cdp import BrowserSniffer, find_browser
from ..downloader import DownloadConfig, M3U8Downloader
from ..fftools import FFMPEG_DOWNLOAD_PAGE, ffmpeg_version, find_ffmpeg
from ..playlist import (
    MasterPlaylist,
    MediaPlaylist,
    format_duration,
    format_size,
    parse_m3u8,
)
from ..recorder import MOBILE_UA, RecordStore
from ..preview import PreviewServer
from ..sniffer import Sniffer, VideoInfo
from ..cookies import ProfileCookies
from ..webinject import READ_JS as READ_PAGE_URLS_JS
from ..webinject import install as install_page_injection
from ..websniff import (
    WEBENGINE_OK,
    VideoUrlInterceptor,
    WebSniffSession,
    rank_candidate_items,
)
from .about import AboutDialog
from .tools_dialog import ToolsDialog, default_state


def _dbg(msg: str) -> None:
    """诊断日志: 写入 %TEMP%/m3u8dl_crash.log(frozen 环境排查退出路径用)。"""
    try:
        p = Path(os.environ.get("TEMP", ".")) / "m3u8dl_crash.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def assets_dir() -> Path:
    """素材目录: 开发时为项目根 assets/, frozen 时为解压目录下的 assets/。

    v1.1.18 修正: 原来 frozen 分支直接返回 ``_MEIPASS``, 而打包参数是
    ``--add-data assets/xxx;assets`` —— 文件其实落在 ``_MEIPASS/assets/``。
    结果 exe 里 icon.ico 与 hls.min.js 全都找不到(窗口图标丢失、嗅探页的
    hls.js 也没加载)。这里优先认 assets 子目录, 找不到再退回根目录。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", ""))  # type: ignore[attr-defined]
        sub = base / "assets"
        return sub if sub.is_dir() else base
    return Path(__file__).resolve().parent.parent.parent / "assets"


def sanitize_name(name: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip().strip(".")
    return safe or "video"


class SnifferWorker(QThread):
    finished = Signal(list)
    failed = Signal(str)
    log = Signal(str)

    def __init__(self, url: str, use_proxy: bool = True,
                 candidate_urls: Optional[List[str]] = None,
                 page_title: str = "",
                 headers_map: Optional[dict] = None):
        super().__init__()
        self.url = url
        self.use_proxy = use_proxy
        self.candidate_urls = candidate_urls   # 浏览器拦截到的候选地址(非 None 时走 sniff_urls)
        self.page_title = page_title
        self.headers_map = headers_map or {}

    def run(self):
        try:
            sniffer = Sniffer(use_proxy=self.use_proxy, log=self.log.emit)
            if self.candidate_urls is not None:
                infos = sniffer.sniff_urls(
                    self.candidate_urls, self.url, self.page_title,
                    headers_map=self.headers_map)
            else:
                infos = sniffer.sniff(self.url)
            self.finished.emit(infos)
        except Exception as e:
            self.failed.emit(str(e))


class BiliWorker(QThread):
    """B站专用解析线程。

    不走网页嗅探: B站是 DASH(音视频两条独立流), 而且接口有 WAF 风控, 直接
    按官方 playurl 接口取, 比抓页面稳得多。详见 :mod:`m3u8dl.bili`。
    """
    finished = Signal(object)        # BiliResult
    failed = Signal(str)
    log = Signal(str)

    def __init__(self, url: str, cookie: str = "", use_proxy: bool = True):
        super().__init__()
        self.url = url
        self.cookie = cookie
        self.use_proxy = use_proxy

    def run(self):
        try:
            from .. import bili

            result = bili.resolve(self.url, cookie=self.cookie,
                                  use_proxy=self.use_proxy,
                                  log=self.log.emit)
            self.finished.emit(result)
        except Exception as e:                # noqa: BLE001 - 线程内兜底
            self.failed.emit(str(e))


class DownloadWorker(QThread):
    progress = Signal(dict)
    log = Signal(str)
    state = Signal(str)
    finished = Signal(bool, str)     # (ok, status)

    def __init__(self, config: DownloadConfig, ffmpeg_path=None):
        super().__init__()
        self.config = config
        self.ffmpeg_path = ffmpeg_path
        self.downloader: Optional[M3U8Downloader] = None

    def run(self):
        dl = M3U8Downloader(
            self.config,
            on_log=self.log.emit,
            on_progress=self.progress.emit,
            on_state=self.state.emit,
            ffmpeg_path=self.ffmpeg_path,
        )
        self.downloader = dl
        try:
            status = dl.run()
            self.finished.emit(status == "done", status)
        except Exception as e:
            self.log.emit(f"下载线程异常: {e}")
            self.finished.emit(False, f"failed: {e}")


class TaskTable(QTableWidget):
    """任务表: 支持拖动重排序(放下后发信号, 由主窗口同步任务队列顺序)。"""

    rows_reordered = Signal()

    def dropEvent(self, event):
        super().dropEvent(event)
        self.rows_reordered.emit()


class _DotsHandle(QSplitterHandle):
    """分栏拖动条: 一条虚线 + 五个点, **两种拖法**。

    默认的 QSplitterHandle 只是一块纯色, 和卡片式布局放在一起显得突兀;
    换成"虚线 + 点阵"后既有明确的拖动提示, 又不会抢视觉重点。点用主色的
    深蓝, 虚线用最淡的分隔色, 但鼠标悬停/拖动时整体提亮。

    v1.1.20 起五点簇是个可抓的"滑块":
    - 在**五点以外**按住拖 = 调左右列宽 / 上下行高(原生行为, 交给 splitter);
    - 在**五点上**按住拖 = 只把五点簇沿虚线滑到新位置(不改宽高)。
    两种手势在同一个 16px 宽的把手上, 靠命中区区分: 簇中心 ±16px 内算"抓点"。
    鼠标停在簇上会换成四向光标并浮出一层淡淡的胶囊底色 —— 否则没人知道
    那几个点能抓。
    """

    _DASH = QColor(0x35, 0x3B, 0x48)
    _DASH_HOVER = QColor(0x6B, 0x74, 0x88)
    _DOT = QColor(0x2F, 0x6B, 0xD8)      # 深蓝: 与主色统一(v1.1.15, 原鎏金)
    _DOT_HOVER = QColor(0x7A, 0xA8, 0xF5)
    _PAD = 12          # 虚线两端留白(与 paintEvent 里一致)
    _GAP = 6.0         # 五点间距
    _GRIP_HALF = 16.0  # 五点簇的拖动命中半长(略大于 2.5*GAP, 更好按)

    #: 五点簇被拖到新位置(0~1, 相对虚线可用长度)
    grip_moved = Signal(float)

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.setMouseTracking(True)   # 不按键也要能收到 move, 用来换光标/提亮
        self._hot = False
        self._pos = 0.5               # 五点簇在虚线长度上的相对位置
        self._on_grip = False         # 鼠标当前是否停在这簇点上
        self._grip_drag = False       # 是否正"抓着这簇点"拖
        self._set_idle_cursor()

    # ------------------------------------------------------------ 几何
    def _set_idle_cursor(self):
        self.setCursor(Qt.SplitHCursor
                       if self.orientation() == Qt.Horizontal else Qt.SplitVCursor)

    def _axis_len(self) -> int:
        return self.height() if self.orientation() == Qt.Horizontal else self.width()

    def _usable(self) -> float:
        return max(1.0, float(self._axis_len() - self._PAD * 2))

    def _grip_center(self) -> float:
        return self._PAD + self._usable() * self._pos

    def _hit_grip(self, pos) -> bool:
        v = pos.y() if self.orientation() == Qt.Horizontal else pos.x()
        return abs(v - self._grip_center()) <= self._GRIP_HALF

    def grip_pos(self) -> float:
        return self._pos

    def set_grip_pos(self, pos: float):
        try:
            pos = float(pos)
        except (TypeError, ValueError):
            return
        self._pos = min(1.0, max(0.0, pos))
        self.update()

    # ------------------------------------------------------------ 交互
    def enterEvent(self, event):
        self._hot = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hot = False
        self._on_grip = False
        if not self._grip_drag:
            self._set_idle_cursor()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        # 抓在五点上 -> 只挪高亮, 不碰宽高(所以不调 super)
        if event.button() == Qt.LeftButton and self._hit_grip(event.position()):
            self._grip_drag = True
            self.setCursor(Qt.ClosedHandCursor)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._grip_drag:
            v = event.position().y() if self.orientation() == Qt.Horizontal \
                else event.position().x()
            self.set_grip_pos((v - self._PAD) / self._usable())
            self.grip_moved.emit(self._pos)
            event.accept()
            return
        # 悬停: 停在簇上就给"可抓"的四向光标 + 胶囊底
        over = self._hit_grip(event.position())
        if over != self._on_grip:
            self._on_grip = over
            self.setCursor(Qt.SizeAllCursor if over else Qt.SplitHCursor)
            if not over:
                self._set_idle_cursor()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._grip_drag:
            self._grip_drag = False
            self._on_grip = self._hit_grip(event.position())
            if self._on_grip:
                self.setCursor(Qt.SizeAllCursor)
            else:
                self._set_idle_cursor()
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            w, h = self.width(), self.height()
            horiz = self.orientation() == Qt.Horizontal
            cx, cy = w / 2.0, h / 2.0

            # 虚线沿拖动方向铺: 左右分栏 -> 竖虚线; 上下分栏 -> 横虚线
            pad = self._PAD
            pen = QPen(self._DASH_HOVER if self._hot else self._DASH)
            pen.setWidth(1)
            pen.setStyle(Qt.CustomDashLine)
            pen.setDashPattern([3, 4])
            p.setPen(pen)
            if horiz:
                p.drawLine(int(cx), pad, int(cx), max(pad, h - pad))
            else:
                p.drawLine(pad, int(cy), max(pad, w - pad), int(cy))

            # 五点簇: 位置由 self._pos 决定(可被用户拖走), 压在虚线上把线"断开"
            center = self._grip_center()
            live = self._hot or self._grip_drag
            gap = self._GAP
            r = 2.0 if not live else 2.4
            if self._grip_drag:
                r = 2.9
            dot = QColor(self._DOT_HOVER if live else self._DOT)

            # 停在簇上时浮一层淡蓝胶囊, 提示"这五个点能抓"
            if self._on_grip and not self._grip_drag:
                cap = QColor(self._DOT)
                cap.setAlpha(46)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cap)
                half = 2.5 * gap + 4
                if horiz:
                    p.drawRoundedRect(
                        QRectF(cx - 7, center - half, 14, half * 2), 7, 7)
                else:
                    p.drawRoundedRect(
                        QRectF(center - half, cy - 7, half * 2, 14), 7, 7)

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(dot)
            for i in range(-2, 3):
                if horiz:
                    oy = center + i * gap
                    p.drawEllipse(QRectF(cx - r, oy - r, r * 2, r * 2))
                else:
                    ox = center + i * gap
                    p.drawEllipse(QRectF(ox - r, cy - r, r * 2, r * 2))
        finally:
            p.end()


class _DotsSplitter(QSplitter):
    """使用「虚线 + 五点」拖动条的 QSplitter。"""

    def createHandle(self):
        return _DotsHandle(self.orientation(), self)

    # ---- 五点簇位置(可拖) -------------------------------------------------
    def grip_pos(self) -> float:
        h = self.handle(1)
        return h.grip_pos() if isinstance(h, _DotsHandle) else 0.5

    def set_grip_pos(self, pos: float):
        h = self.handle(1)
        if isinstance(h, _DotsHandle):
            h.set_grip_pos(pos)


class _TickBox(QCheckBox):
    """自绘的复选框: 圆角底 + 白色对勾。

    为什么不用 QSS: 一旦给 ``QCheckBox::indicator`` 设了 background/border,
    Qt 就不再画系统的对勾, 结果只剩一个**纯色方块**(看起来像"开关没开")。
    QSS 里要补对勾只能外挂图片资源, 打包后路径容易丢; 这里直接画, 无资源依赖。

    两种配色:
    - 默认紫色: 兜底配色(目前无处使用)
    - ``accent=True`` 深蓝: 全部勾选项统一走这套(v1.1.14 起下载设置四个,
      v1.1.15 起预览控制条的「静音」也并入)
    """

    _ON = QColor(0x7C, 0x5C, 0xD6)      # 紫
    _ON_HOT = QColor(0x8E, 0x70, 0xE2)
    _ACCENT = QColor(0x1E, 0x40, 0xAF)       # 深蓝(主色)
    _ACCENT_HOT = QColor(0x2F, 0x6B, 0xD8)
    _OFF_BG = QColor(0x1D, 0x21, 0x29)
    _OFF_BD = QColor(0x4A, 0x52, 0x65)

    def __init__(self, text: str = "", parent=None, accent: bool = False):
        super().__init__(text, parent)
        self._hot = False
        self._on = self._ACCENT if accent else self._ON
        self._on_hot = self._ACCENT_HOT if accent else self._ON_HOT
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, event):
        self._hot = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hot = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            box = 16.0
            y = (self.height() - box) / 2.0
            r = QRectF(0.5, y + 0.5, box - 1, box - 1)
            if self.isChecked():
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(self._on_hot if self._hot else self._on)
                p.drawRoundedRect(r, 4, 4)
                pen = QPen(QColor("#FFFFFF"))
                pen.setWidthF(2.0)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                # 对勾两笔: 短的在左下, 长的往右上
                p.drawLine(QPointF(4.0, y + 8.4), QPointF(7.0, y + 11.4))
                p.drawLine(QPointF(7.0, y + 11.4), QPointF(12.4, y + 5.0))
            else:
                p.setPen(QPen(self._OFF_BD))
                p.setBrush(self._OFF_BG)
                p.drawRoundedRect(r, 4, 4)
            p.setPen(QColor("#E6E3DD" if self.isEnabled() else "#5A5F6A"))
            p.drawText(QRectF(box + 8, 0, self.width() - box - 8, self.height()),
                       int(Qt.AlignLeft | Qt.AlignVCenter), self.text())
        finally:
            p.end()


#: 左右列默认宽度(对应窗口默认 650 宽)。网址栏上移到通栏后, 左列只剩
#: 进阶入口 + 视频列表 + 预览, 右列只剩下载设置表单, 两列都压得比较窄。
#: 右列表单的最小宽度约 336, 所以右列给 340、左列吃剩下的 ~290。
DEFAULT_SPLIT_SIZES = (300, 340)
#: 任务表 / 日志 竖向分割的默认高度(相对权重 1:1 = 等分, 见 _bottom_split_sizes)
DEFAULT_BOTTOM_SPLIT_SIZES = (1, 1)
#: 五点簇在拖动条上的默认位置(0.5 = 正中; 可按住五点拖到任意比例, 见 _DotsHandle)
DEFAULT_GRIP_POS = 0.5


class MainWindow(QMainWindow):
    #: 数据发送是后台线程跑的, 用信号回主线程更新界面
    dataSent = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{version.APP_NAME} {version.VERSION}")
        # 默认 650x900: 窄而高, 贴边放不挡桌面。网址栏已上移到通栏, 地址框
        # 不再被列宽裁掉; 最小 620 时左右列仍各自可用。
        self.setMinimumSize(620, 740)
        self.resize(650, 940)
        icon = assets_dir() / "icon.ico"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        self.ffmpeg_path = find_ffmpeg()
        self.preview = PreviewServer(assets_dir() / "hls.min.js")
        self.preview.start()

        self.sniffer_worker: Optional[SnifferWorker] = None
        #: B站 解析线程(DASH 专用通路, 与网页嗅探互斥)
        self.bili_worker: Optional[BiliWorker] = None
        self.download_worker: Optional[DownloadWorker] = None
        self.current_info: Optional[VideoInfo] = None
        self.infos = []

        # 播放器(FFmpeg 后端, 具备 H.264/AAC 解码能力)
        self.player = None
        self.audio = None

        # 浏览器引擎嗅探设施(复刻猫抓的网络请求拦截):
        # 静态 HTML 正则抓不到 JS 动态加载的视频(如 Stripchat 类直播站),
        # 此时自动回退到内置 Chromium 引擎真实加载页面并拦截网络请求。
        self._web_sniff_url = ""
        self._web_sniff_title = ""
        self._web_sniff_used = False    # 本次手动嗅探是否已回退过(防循环)
        self._profile = None
        if WEBENGINE_OK:
            profile = QWebEngineProfile.defaultProfile()
            self._profile = profile
            # 关键: QtWebEngine 缺 H.264/AAC 解码器, 站点播放器能力检测不过就
            # 不发 m3u8 请求。注入脚本伪装编解码器支持, 让播放器正常发起请求。
            self._inject_ok = install_page_injection(profile)
            # Cookie 罐: 带防盗链的 CDN 需要浏览器登录态, 解析/下载时回放
            self._cookies = ProfileCookies(profile, self)
            self._url_interceptor = VideoUrlInterceptor()
            profile.setUrlRequestInterceptor(self._url_interceptor)
            self._web_sniff = WebSniffSession(self._url_interceptor, self)
            self._url_interceptor.hit.connect(self._on_web_hit)
            self._web_sniff.page_loaded.connect(self._on_web_page_loaded)
            self._web_sniff.finished.connect(self._on_web_sniff_finished)
        else:
            self._inject_ok = False
            self._cookies = None
            self._url_interceptor = None
            self._web_sniff = None

        # ---- 系统浏览器嗅探(CDP) ----
        # 内置 QtWebEngine 是开源编译版: 无 H.264/AAC 解码器, 且进不去跨域
        # iframe 里的播放器 —— 有些站点在内置引擎里永远不请求视频。
        # 系统浏览器(Edge/Chrome)通过调试协议监听, 拿到的是真浏览器发出的
        # 请求(含真实 Referer/Cookie), 与猫抓完全等价。
        self._cdp = BrowserSniffer(self)
        self._cdp.captured.connect(self._on_cdp_hit)
        self._cdp.status.connect(self._log_ui)
        self._cdp.finished.connect(self._on_cdp_finished)
        self._cdp_items: list = []      # 收集到的候选(退出时统一解析)
        self._cdp_url = ""              # 本次交给系统浏览器的页面地址
        self._closing = False           # 正在退出: 回调里不再弹窗
        self._bottom_split_ready = False  # 竖向分割的首帧尺寸是否已应用

        # ---- 深度搜索 / 缓存捕捉 / 更多功能 ----
        self._deep_keys = []            # 深度搜索捞到的疑似密钥 [{k,t,from}]
        self._deep_texts = []           # 深度搜索捞到的 m3u8 原文
        self.adv = default_state()      # 更多功能选项(直接映射到 DownloadConfig)
        self._cap_sid = f"{os.getpid()}{int(time.time()) % 100000}"
        self._capture = CaptureStore(
            Path(tempfile.gettempdir()) / "m3u8dl_capture")
        self._cap_on = False
        self._bridge = None
        self._cap_stats = {}
        self._cap_timer = QTimer(self)
        self._cap_timer.setInterval(1000)
        self._cap_timer.timeout.connect(self._poll_capture_stats)

        # ---- 录制 / 自动化（逆向自猫抓的 6 个按钮）----
        self._rec = RecordStore(Path(tempfile.gettempdir()) / "m3u8dl_record")
        self._rec_kind = ""            # "" | element | webrtc | screen
        self._rec_mime = ""
        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(1000)
        self._rec_timer.timeout.connect(self._poll_rec_stats)
        self._desktop_ua = ""          # 桌面 UA（模拟手机后要能还原）
        self._mobile_on = False
        self.auto_download = False
        self.adblock_on = True         # 广告拦截（与拦截器默认值保持一致）
        self._auto_done = set()        # 已自动下载过的地址(避免重复入队)

        # 任务队列
        self._tasks = {}          # id -> dict(info, cfg, state, progress)
        self._task_order = []     # [id]
        self._next_task_id = 1
        self._active_task_id = None

        self._build_ui()
        self._refresh_ffmpeg_status()      # 启动优先自动检测 ffmpeg 环境
        self.ed_url.setFocus()             # 初始焦点落在网址栏, 打开即可粘贴
        # ---- 深度搜索 / 缓存捕捉: 页面脚本 + 数据桥（必须在播放器建 SourceBuffer 前注入）----
        if WEBENGINE_OK and self._profile is not None:
            self._setup_deep_and_capture()
        if self._web_sniff is not None:
            # 浏览器视图的加载完成事件驱动浏览器嗅探会话
            self.web.page().loadFinished.connect(
                self._web_sniff.notify_page_loaded)
        self._log_ui(f"ffmpeg: {self.ffmpeg_path or '未检测到(TS 二进制合并不受影响)'}")
        self._log_ui(f"预览服务已启动: 127.0.0.1:{self.preview.port}")
        if MEDIA_OK:
            self._log_ui("预览引擎: QtMultimedia / FFmpeg 后端(H.264/AAC 可解码)")
        else:
            self._log_ui("预览引擎不可用: 缺少 QtMultimedia, 预览功能已禁用")
        if WEBENGINE_OK:
            self._log_ui(
                "浏览器嗅探: 已注入编解码器能力伪装"
                + ("(成功)" if self._inject_ok else "(失败, 可能影响嗅探)"))
        try:
            from ..diag import log as _diag
            _diag(f"components: media_ok={MEDIA_OK} webengine_ok={WEBENGINE_OK} "
                  f"inject_ok={self._inject_ok} preview_port={self.preview.port} "
                  f"ffmpeg={self.ffmpeg_path}")
        except Exception:
            pass

    # ================================================================ UI 构建
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 6)
        root.setSpacing(7)

        # 顶部: ffmpeg 环境状态(启动即自动检测) + 快捷操作
        root.addWidget(self._build_top_strip())

        # 顶部通栏: 网址栏。原先挤在左列里, 地址稍长就被裁掉("https://" 少了 h),
        # 且三个按钮占满一行。上移到通栏后地址框能吃满窗口宽度。
        root.addWidget(self._build_url_bar())

        # 主分割: 左(进阶入口 + 视频列表 + 预览/浏览器) | 右(下载设置)
        main_split = _DotsSplitter(Qt.Horizontal)
        self.main_split = main_split
        main_split.setChildrenCollapsible(False)
        main_split.setHandleWidth(16)          # 给虚线 + 五点留出呼吸空间
        main_split.addWidget(self._build_left_panel())
        main_split.addWidget(self._build_right_panel())
        # 伸展给**左列**: 视频列表和预览越宽越好用; 右列是固定宽度的设置表单
        # (已设最大宽度), 拉宽只会让输入框变成一条跑道。
        main_split.setStretchFactor(0, 1)
        main_split.setStretchFactor(1, 0)
        main_split.setSizes(self._split_sizes())
        main_split.set_grip_pos(self._grip_pos("ui/grip_lr"))
        root.addWidget(main_split, 1)

        # 底部通栏: 下载进度 + (任务表 / 日志 竖向分割)
        # 进度条跟着任务表走(在它上侧), 看的是"当前这个文件"的整体进度。
        root.addWidget(self._build_progress_group())
        # 任务表与日志之间也放一条五点拖动条: 行高不写死, 用户自己拉
        root.addWidget(self._build_bottom_split())

        # ---- 状态栏: 版本信息, 点击弹出「关于」----
        # 窗口默认只有 650 宽, 完整长句会把末尾挤到看不见, 所以状态栏用紧凑写法
        # (© 2026 肆月Aperture), 全文挂在 tooltip 与「关于」对话框里。
        _copy = version.COPYRIGHT.replace("Copyright ", "")
        self.lbl_version = QPushButton(
            f"{version.APP_NAME} {version.VERSION} (Build {version.BUILD})"
            f"  |  {_copy}  |  开源软件，禁止商用")
        self.lbl_version.setFlat(True)
        # 不可聚焦: 避免抢占初始焦点(应落在网址栏), 也避免焦点框在文字尾部
        # 渲染出一条竖线
        self.lbl_version.setFocusPolicy(Qt.NoFocus)
        self.lbl_version.setCursor(Qt.PointingHandCursor)
        self.lbl_version.setStyleSheet(
            "QPushButton{border:none; background:transparent; text-align:left;"
            " padding:0 2px; color:#b0a89a;}"
            "QPushButton:hover{color:#c9a45c; text-decoration:underline;}")
        self.lbl_version.setToolTip(
            f"{version.LICENSE_NOTE}\n点击查看关于信息")
        self.lbl_version.clicked.connect(self._show_about)
        self.statusBar().addWidget(self.lbl_version)

        # 首帧之后按真实宽度再收一次列: __init__ 里的 resize(650,900) 早于
        # 控件创建, 那次的 resizeEvent 里 self.tree 还不存在。
        QTimer.singleShot(0, self._fit_tree_columns)

    def _build_top_strip(self) -> QWidget:
        """顶部状态条: ffmpeg 环境(启动自动检测) + 重新检测 / 清理缓存。"""
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        self.lbl_ffmpeg = QLabel("ffmpeg: 检测中…")
        self.lbl_ffmpeg.setObjectName("hintLabel")
        self.btn_ffmpeg = QPushButton("重新检测")
        self.btn_ffmpeg.setFixedWidth(88)
        self.btn_ffmpeg.clicked.connect(self._check_ffmpeg_manual)
        self.btn_cache = QPushButton("清理缓存")
        self.btn_cache.setFixedWidth(88)
        self.btn_cache.setToolTip("清理内置浏览器累积的网页缓存(保留登录 Cookie)")
        self.btn_cache.clicked.connect(self._clear_web_cache)
        h.addWidget(self.lbl_ffmpeg, 1)
        h.addWidget(self.btn_ffmpeg)
        h.addWidget(self.btn_cache)
        return w

    def _refresh_ffmpeg_status(self):
        """检测并展示 ffmpeg 环境(启动时第一件事)。"""
        p = find_ffmpeg()
        self.ffmpeg_path = p
        if p is None:
            self.lbl_ffmpeg.setText(
                "● 未检测到 ffmpeg —— TS 分片仍可用二进制合并, 输出 MP4 需要 ffmpeg")
            self.lbl_ffmpeg.setToolTip("点击右侧「重新检测」重试")
            self.lbl_ffmpeg.setStyleSheet("color:#d98c3f;")
            return
        ver = ffmpeg_version(p) or ""
        brief = " ".join(ver.split()[:3]) if ver else Path(p).name
        self.lbl_ffmpeg.setText(f"● ffmpeg 已就绪 · {brief}")
        self.lbl_ffmpeg.setToolTip(f"{ver}\n\n{p}")
        self.lbl_ffmpeg.setStyleSheet("color:#6fbf73;")

    # ---------------------------------------------------------------- 顶部通栏
    def _build_url_bar(self) -> QWidget:
        """顶部通栏网址栏: 网址输入框吃满宽度, 右侧紧跟三个动作按钮。

        以前它嵌在左列里(左列仅 430px), 地址稍长就被裁 —— 用户看到的是
        ``ttps://livecamrips.to/...``(开头的 h 被滚出可视区)。上移到通栏后
        输入框能随窗口宽度伸缩。
        """
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.ed_url = QLineEdit()
        self.ed_url.setPlaceholderText("网址 / m3u8 直链…")
        self.ed_url.returnPressed.connect(self._on_sniff)
        self.btn_sniff = QPushButton("解析网页")
        self.btn_sniff.setObjectName("primaryBtn")
        self.btn_sniff.clicked.connect(self._on_sniff)
        self.btn_stop_sniff = QPushButton("停止")
        self.btn_stop_sniff.setEnabled(False)
        self.btn_stop_sniff.clicked.connect(self._on_stop_sniff)
        # 系统浏览器嗅探: 内置引擎的两处天然局限(无 H.264 解码器 / 进不去
        # 跨域 iframe)只有真浏览器能绕开 —— 页面在 Edge/Chrome 里打开,
        # 我们通过调试协议只听请求, 用户点一下播放即可抓到。
        self.btn_sys_browser = QPushButton("系统浏览器")
        self.btn_sys_browser.setCheckable(True)
        bname, bexe = find_browser()
        if bexe:
            self.btn_sys_browser.setToolTip(
                f"用本机 {bname} 打开页面并监听它的全部网络请求(CDP)。\n"
                "内置浏览器缺 H.264 解码器、且无法进入跨域 iframe 中的播放器,\n"
                "部分站点因此永远抓不到地址 —— 改用真浏览器即可。\n"
                "点一下按钮启动, 在浏览器里点播放, 再点一次结束并解析。")
        else:
            self.btn_sys_browser.setEnabled(False)
            self.btn_sys_browser.setToolTip("未检测到 Edge / Chrome")
        self.btn_sys_browser.toggled.connect(self._on_sys_browser_toggle)
        row.addWidget(QLabel("网址:"))
        row.addWidget(self.ed_url, 1)
        row.addWidget(self.btn_sniff)
        row.addWidget(self.btn_stop_sniff)
        row.addWidget(self.btn_sys_browser)
        return w

    def _build_adv_panel(self) -> QGroupBox:
        """三件进阶武器(逆向自猫抓): 深度搜索 / 缓存捕捉 / 更多功能。

        v1.1.13: 从左侧列移到**右侧列顶部**。它们操作的是「浏览器嗅探」页签里的
        页面, 和左列的视频列表没有从属关系; 挪走后左列整块让给列表与预览。
        v1.1.14: 用户要求「和下载设置一样外面加个框」—— 改回 QGroupBox,
        标题「进阶功能」, 与「下载设置」同一套 QSS(标题鎏金、圆角描边)。
        纵向多花 ~34px, 已从右列表单里挤回来(见 _build_settings_group)。
        """
        grp = QGroupBox("进阶功能")
        gv = QVBoxLayout(grp)
        gv.setSpacing(4)
        adv_box = QHBoxLayout()
        adv_box.setSpacing(5)
        self.btn_deep = QPushButton("深度搜索")
        self.btn_deep.setObjectName("advBtn")
        self.btn_deep.setToolTip(
            "钩住页面内存里的取址/解码函数, 捞取从不走网络的 m3u8、分片地址与\n"
            "疑似 AES KEY(对应猫抓「深度搜索」)")
        self.btn_deep.clicked.connect(self._on_deep_search)
        self.btn_cap = QPushButton("缓存捕捉")
        self.btn_cap.setObjectName("advBtn")
        self.btn_cap.setCheckable(True)
        self.btn_cap.setToolTip(
            "代理 MediaSource.appendBuffer, 直接留存播放器送进解码器的已解密裸\n"
            "字节, 绕开 AES / 图片伪装 / 分片 token(对应猫抓「缓存捕捉」)")
        self.btn_cap.toggled.connect(self._on_capture_toggle)
        self.btn_cap_save = QPushButton("保存")
        # 单独的窄内边距: 它一出现这一行就变 4 个按钮, 用 15px 内边距会把
        # 右列最小宽度顶到 365px(650 窗口塞不下)。2 个字的按钮不需要那么宽。
        self.btn_cap_save.setObjectName("advBtnSm")
        self.btn_cap_save.setToolTip("把已捕获的数据写入工作目录(多路自动合成)")
        self.btn_cap_save.setVisible(False)
        self.btn_cap_save.clicked.connect(self._save_capture)
        self.btn_adv = QPushButton("更多功能")
        self.btn_adv.setObjectName("advBtn")
        self.btn_adv.setToolTip(
            "分片正则筛选 / 反选 / 追加分片参数 / 剥离图片伪装 / 跳过解密 /\n"
            "仅音频 / 疑似密钥验证(对应猫抓「更多功能」)")
        self.btn_adv.clicked.connect(self._on_tools)
        self.lbl_adv = QLabel("先在「浏览器嗅探」页签打开页面")
        self.lbl_adv.setObjectName("hintLabel")
        # 允许被压缩: 否则这行状态文字会把右列撑宽
        self.lbl_adv.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_adv.setMinimumWidth(0)
        self.lbl_adv.setWordWrap(True)
        adv_box.addWidget(self.btn_deep)
        adv_box.addWidget(self.btn_cap)
        adv_box.addWidget(self.btn_cap_save)
        adv_box.addWidget(self.btn_adv)
        adv_box.addStretch(1)
        gv.addLayout(adv_box)
        gv.addWidget(self.lbl_adv)
        return grp

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)

        # ---- 检测到的视频 ----
        v.addWidget(QLabel("检测到的视频(单击预览并自动填充, 双击直接下载)"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["标题", "清晰度", "码率", "时长", "估算大小"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(120)
        hh = self.tree.header()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self._on_tree_select)
        self.tree.itemDoubleClicked.connect(self._on_tree_double_click)
        v.addWidget(self.tree, 2)
        # ---- 预览 / 浏览器 双页签 ----
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_preview_page(), "实时预览")
        self.tabs.addTab(self._build_browser_page(), "浏览器嗅探")
        v.addWidget(self.tabs, 3)
        return w

    def _build_preview_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)

        self.pv_stack = None
        self.video_widget = None
        self.lbl_pv_hint = None
        if MEDIA_OK:
            # 黑底容器: QVideoWidget 在无媒体时不绘制背景, 用外层容器保证
            # 「播放器」观感, 并把空态提示居中叠在画面区域上。
            box = QFrame()
            box.setObjectName("pvBox")
            box.setMinimumHeight(120)
            # v1.1.15: 去掉 1px 描边 —— 暗底上那圈浅灰线和"黑画面"割裂,
            # 用户两次反馈"外框没去掉"。改 none 后只剩纯黑圆角块。
            box.setStyleSheet(
                "#pvBox{background:#000000; border:none; border-radius:6px;}")
            self.pv_stack = QStackedLayout(box)
            self.pv_stack.setContentsMargins(0, 0, 0, 0)

            self.video_widget = QVideoWidget()
            self.pv_stack.addWidget(self.video_widget)

            self.lbl_pv_hint = QLabel(
                "选中左侧视频后在此实时预览\n(双击视频条目可直接下载)")
            self.lbl_pv_hint.setAlignment(Qt.AlignCenter)
            self.lbl_pv_hint.setStyleSheet(
                "color:#6e6e6e; background:transparent; border:none;")
            self.pv_stack.addWidget(self.lbl_pv_hint)
            self.pv_stack.setCurrentIndex(1)
            v.addWidget(box, 1)

            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
            try:
                self.player.setVideoOutput(self.video_widget)
            except Exception:
                pass
            self.player.errorOccurred.connect(self._on_player_error)

            bar = QHBoxLayout()
            bar.setSpacing(5)
            self.btn_play = QPushButton("▶ 播放")
            self.btn_play.setObjectName("pvBtn")
            self.btn_play.clicked.connect(self._toggle_play)
            self.btn_pv_stop = QPushButton("■ 停止")
            self.btn_pv_stop.setObjectName("pvBtn")
            self.btn_pv_stop.clicked.connect(self._stop_preview)
            # v1.1.15: 静音勾选框跟随主色改深蓝(原紫色与整体配色不统一)
            self.ck_mute = _TickBox("静音", accent=True)
            self.ck_mute.setChecked(True)
            self.ck_mute.toggled.connect(self._on_mute)
            self.sl_vol = QSlider(Qt.Horizontal)
            self.sl_vol.setRange(0, 100)
            self.sl_vol.setValue(70)
            self.sl_vol.setMaximumWidth(110)
            # 不设最小值时, 左列一窄滑块就被压成 30px 的小疙瘩;
            # 但设太大又会把左列最小宽度顶到 300+, 650 窗口就装不下了
            self.sl_vol.setMinimumWidth(40)
            self.sl_vol.valueChanged.connect(self._on_volume)
            bar.addWidget(self.btn_play)
            bar.addWidget(self.btn_pv_stop)
            bar.addStretch(1)
            bar.addWidget(self.ck_mute)
            bar.addWidget(self.sl_vol)
            v.addLayout(bar)
            self._on_mute(True)
            self._on_volume(self.sl_vol.value())
        else:
            hint = QLabel("当前环境缺少 QtMultimedia 组件, 预览不可用"
                          "(不影响嗅探与下载)")
            hint.setObjectName("hintLabel")
            hint.setWordWrap(True)
            v.addWidget(hint, 1)

        self.lbl_info = QLabel("选中左侧视频后在此实时预览")
        self.lbl_info.setObjectName("hintLabel")
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setVisible(not MEDIA_OK)
        v.addWidget(self.lbl_info)
        return w

    def _show_pv_placeholder(self):
        """回到预览空态(黑色画面区 + 居中提示)。"""
        if self.pv_stack is not None:
            self.pv_stack.setCurrentIndex(1)

    def _build_browser_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        self.web = QWebEngineView()
        self.web.setUrl(QUrl("about:blank"))
        self.web.setMinimumHeight(120)
        v.addWidget(self.web, 1)
        self.lbl_web = QLabel(
            "页面在此加载, 视频地址由网络请求拦截 + 页面脚本钩子自动捕获。")
        self.lbl_web.setObjectName("hintLabel")
        self.lbl_web.setWordWrap(True)
        v.addWidget(self.lbl_web)
        return w

    # ---------------------------------------------------------------- 右列
    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        # 下限 320: 表单自身的最小宽度约 336, 但 650 窄窗下要和左列(约 292)
        # 一起塞进 614 的可用宽度 —— 显式给一个更小的下限, 让输入框自己缩。
        w.setMinimumWidth(320)
        # 上限 430: 这是一列"标签 + 输入框"的表单, 再宽只是把输入框拉成跑道。
        # 给上限后 QSplitter 会把多余宽度交给左列(视频列表/预览), 正是我们要的。
        w.setMaximumWidth(430)
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(7)
        v.addWidget(self._build_adv_panel())
        # 拉伸因子 1 + 不留尾部 stretch: 「下载设置」框会撑满剩余高度,
        # 底边与左列的页签框对齐(v1.1.14 用户要求左右默认高度一致)。
        v.addWidget(self._build_settings_group(), 1)
        return w

    def _build_settings_group(self) -> QGroupBox:
        grp = QGroupBox("下载设置")
        # ---- 为什么改成 QGridLayout ----
        # 原来是「一堆 QHBoxLayout 竖直叠起来」: 每行的标签各按自己的字宽占位,
        # 「工作目录/视频标题/保存名称」是 4 个字(52px), 而「线程数/请求头」只有
        # 3 个字(39px)、「超时」2 个字(26px) —— 于是它们的输入框左边缘跟着往左跑
        # (实测 75 / 62 / 50), 表单左边缘是锯齿状的(用户反馈"框和上方不对齐")。
        # 网格把第 0 列统一成「最宽标签」的宽度, 整个表单的输入框从同一条竖线开始。
        gv = QGridLayout(grp)
        gv.setContentsMargins(9, 9, 9, 9)
        gv.setHorizontalSpacing(6)
        # 行距 3: 这一列有 10 行, 行距每省 1px 就是 10px。v1.1.14 加了
        # 「进阶功能」外框(+34px)后 650×940 差点装不下, 从这里挤回来。
        gv.setVerticalSpacing(3)
        gv.setColumnStretch(1, 1)      # 第 1 列(输入框)吃掉多余宽度
        r = 0

        # 行 0: 工作目录 [输入框] [选择…] —— 按钮单独占第 2 列
        gv.addWidget(QLabel("工作目录"), r, 0)
        self.ed_workdir = QLineEdit(str(Path.home() / "Desktop"))
        # 光标归零: 否则窄输入框会滚到尾部, 开头那段盘符被滚出可视区
        self.ed_workdir.setCursorPosition(0)
        btn_dir = QPushButton("选择…")
        btn_dir.clicked.connect(self._choose_dir)
        gv.addWidget(self.ed_workdir, r, 1)
        gv.addWidget(btn_dir, r, 2)
        r += 1

        # 行 1/2: 标题与保存名 —— 跨到第 2 列, 右边缘和「选择…」按钮齐平
        gv.addWidget(QLabel("视频标题"), r, 0)
        self.ed_title = QLineEdit()
        self.ed_title.setPlaceholderText("选中后自动填写")
        gv.addWidget(self.ed_title, r, 1, 1, 2)
        r += 1

        gv.addWidget(QLabel("保存名称"), r, 0)
        self.ed_save = QLineEdit()
        self.ed_save.setPlaceholderText("不含后缀")
        gv.addWidget(self.ed_save, r, 1, 1, 2)
        r += 1

        # 行 3: 线程数 / 重试 —— 做成一个子布局跨 1~2 列
        # v1.1.17: 这两行必须**共用同一套列宽**, 否则「超时」框和「线程数」框、
        # 「重试」和「限速」、两个数字框全都不在一列上(实测 重试 label 164 /
        # 限速 148, 重试框 196 / 限速框 178)。做法: 四个 spinbox 一律固定宽度,
        # 两个次标签也固定宽度, 行内 spacing 统一 6 —— 三处自然对齐:
        #   spin(70) │6│ label(28) │6│ spin(70) │6│ KB/s
        SPIN_W = 70
        LBL_W = 28
        gv.addWidget(QLabel("线程数"), r, 0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.sp_threads = QSpinBox()
        self.sp_threads.setRange(1, 64)
        self.sp_threads.setValue(16)
        self.sp_threads.setFixedWidth(SPIN_W)
        self.sp_retry = QSpinBox()
        self.sp_retry.setRange(0, 99)
        self.sp_retry.setValue(15)
        self.sp_retry.setFixedWidth(SPIN_W)
        lb_retry = QLabel("重试")
        lb_retry.setFixedWidth(LBL_W)
        row.addWidget(self.sp_threads)
        row.addWidget(lb_retry)
        row.addWidget(self.sp_retry)
        row.addStretch(1)
        gv.addLayout(row, r, 1, 1, 2)
        r += 1

        # 行 4: 超时 / 限速 —— 列宽、间距与上一行完全一致
        gv.addWidget(QLabel("超时"), r, 0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(1, 120)
        self.sp_timeout.setValue(10)
        self.sp_timeout.setFixedWidth(SPIN_W)
        self.sp_speed = QSpinBox()
        self.sp_speed.setRange(0, 102400)
        self.sp_speed.setValue(0)
        # 后缀写进 spinbox 会把最小宽度顶到 140+(它按最宽值 "102400 KB/s" 算),
        # 650 窄窗下这一行就成了"右列最小宽度"的瓶颈 —— 改成旁边一个窄标签。
        self.sp_speed.setFixedWidth(SPIN_W)
        lb_speed = QLabel("限速")
        lb_speed.setFixedWidth(LBL_W)
        row.addWidget(self.sp_timeout)
        row.addWidget(lb_speed)
        row.addWidget(self.sp_speed)
        row.addWidget(QLabel("KB/s"))
        row.addStretch(1)
        gv.addLayout(row, r, 1, 1, 2)
        r += 1

        # 行 5: 请求头
        gv.addWidget(QLabel("请求头"), r, 0)
        self.ed_headers = QLineEdit()
        self.ed_headers.setPlaceholderText("key:value, 多个用 | 分隔")
        gv.addWidget(self.ed_headers, r, 1, 1, 2)
        r += 1

        # 行 6~9: 勾选与按钮 —— 不从标签列开始, 整行跨 3 列
        # 四个勾选压成两行(650 窄窗下右列每多一行就要多吃 30px 纵向空间)
        self.ck_binmerge = _TickBox("二进制合并(快)", accent=True)
        self.ck_binmerge.setToolTip(
            "TS / fMP4 直接硬拼, 最快; 需要转封装(remux)时请关掉它")
        self.ck_binmerge.setChecked(True)
        self.ck_del = _TickBox("合并后删除分片", accent=True)
        self.ck_del.setChecked(True)
        self.ck_nomerge = _TickBox("下载后不合并", accent=True)
        self.ck_noproxy = _TickBox("不使用系统代理", accent=True)
        # v1.1.19: 两行原来各是独立的 QHBoxLayout, 第二个勾选框各按自己标签
        # 宽度起步 —— 「合并后删除分片」比「不使用系统代理」短 7px, 两行的
        # 第二列就差了 7px(肉眼可见的错位)。改成 2×2 网格, 第 0 列按最宽的
        # 「二进制合并(快)」统一, 第二列自然落在同一条竖线上; 列间距给 18
        # (原来是布局退化后的 6, 两个勾选框几乎贴死)。
        cb = QGridLayout()
        cb.setContentsMargins(0, 0, 0, 0)
        cb.setHorizontalSpacing(18)
        cb.setVerticalSpacing(3)
        cb.addWidget(self.ck_binmerge, 0, 0)
        cb.addWidget(self.ck_del, 0, 1)
        cb.addWidget(self.ck_nomerge, 1, 0)
        cb.addWidget(self.ck_noproxy, 1, 1)
        cb.setColumnStretch(2, 1)
        gv.addLayout(cb, r, 0, 1, 3)
        r += 1

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.ck_range = _TickBox("范围选择", accent=True)
        self.ed_rstart = QLineEdit("00:00:00")
        self.ed_rstart.setMaximumWidth(82)
        self.ed_rend = QLineEdit("00:00:00")
        self.ed_rend.setMaximumWidth(82)
        row.addWidget(self.ck_range)
        row.addWidget(self.ed_rstart)
        row.addWidget(QLabel("—"))
        row.addWidget(self.ed_rend)
        row.addStretch(1)
        gv.addLayout(row, r, 0, 1, 3)
        r += 1

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        # v1.1.17: 三个按钮原来紧贴在一起(间隙 0 —— 子布局的 spacing 会退化成 0)
        row.setSpacing(8)
        self.btn_download = QPushButton("开始下载")
        self.btn_download.setObjectName("primaryBtn")
        self.btn_download.clicked.connect(self._on_download)
        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._on_pause)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("dangerBtn")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop_download)
        row.addWidget(self.btn_download)
        row.addWidget(self.btn_pause)
        row.addWidget(self.btn_stop)
        row.addStretch(1)
        gv.addLayout(row, r, 0, 1, 3)
        # 表单行是"贴顶"的, 多余高度全给最后一行会让按钮掉到底部 —— 补一条
        # 纵向弹簧, 让整组控件集中在框体上部
        gv.setRowStretch(r + 1, 1)
        return grp

    def _build_progress_group(self) -> QGroupBox:
        """当前文件的整体进度(通栏, 位于任务列表上侧)。

        刻意**不显示分片计数**(``12/340 分片``): 用户关心的是"这个文件下了多少",
        分片数只对调试有意义, 任务表里已经有每行的进度列。这里给的是
        百分比 + 已下载/总大小 + 速度 + 剩余时间。
        """
        grp = QGroupBox("下载进度")
        # v1.1.16: 这个框只有两行(进度条 + 文案), 用全局 QGroupBox 的
        # padding(14/10/10/10) 会「就绪」下面空出一大截(实测 21px: 布局下边距
        # 9 + 样式下内边距 12)。单独给它 objectName, 在 theme 里把下内边距压到 2。
        grp.setObjectName("progGroup")
        gv = QVBoxLayout(grp)
        gv.setContentsMargins(9, 8, 9, 0)
        gv.setSpacing(4)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.lbl_prog = QLabel("就绪")
        self.lbl_prog.setWordWrap(True)
        gv.addWidget(self.progress)
        gv.addWidget(self.lbl_prog)
        return grp

    # ---------------------------------------------------------------- 底部
    def _build_task_area(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        v.addWidget(QLabel("任务列表(右键打开目录/视频 · 可多选清空 · 可拖动排序)"))
        self.task_table = TaskTable(0, 5)
        self.task_table.setHorizontalHeaderLabels(
            ["任务", "清晰度", "进度", "速度", "状态"])
        self.task_table.verticalHeader().setVisible(False)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.task_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.task_table.setAlternatingRowColors(True)
        self.task_table.setDragDropMode(QAbstractItemView.InternalMove)
        self.task_table.setDefaultDropAction(Qt.MoveAction)
        self.task_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.task_table.customContextMenuRequested.connect(self._on_task_menu)
        self.task_table.rows_reordered.connect(self._sync_task_order)
        th = self.task_table.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            th.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        # 高度交给外层竖向分割条(见 _build_bottom_split), 这里只留一个下限,
        # 免得拖到 0 行连表头都看不见
        self.task_table.setMinimumHeight(64)
        v.addWidget(self.task_table, 1)
        return w

    def _build_bottom_split(self) -> QSplitter:
        """任务表 / 日志之间的竖向分割条(带五点)。

        两边的行高不再写死: 日志占太多就往上拖, 想多盯几行任务就往下拖。
        位置存进 ``ui/bottom_split``, 下次启动保持上次的手感。
        """
        sp = _DotsSplitter(Qt.Vertical)
        self.bottom_split = sp
        sp.setChildrenCollapsible(False)
        sp.setHandleWidth(16)          # 与主分割条同宽, 五点的视觉重量才一致
        sp.addWidget(self._build_task_area())
        sp.addWidget(self._build_log_area())
        sp.setStretchFactor(0, 1)
        sp.setStretchFactor(1, 1)
        sp.setSizes(self._bottom_split_sizes())
        sp.set_grip_pos(self._grip_pos("ui/grip_tb"))
        return sp

    def _bottom_split_sizes(self):
        """任务表/日志的初始高度。

        默认给 ``[1, 1]`` 而不是像素值: QSplitter 在尺寸不足或超出时按"相对
        权重"放大/缩小, 写成 1:1 才能保证首次启动是**等分**; 写 (100, 100)
        在第一次 layout 时会被各控件的 sizeHint 带偏(实测 172:59)。
        """
        try:
            saved = self._user_settings().value("ui/bottom_split")
            if saved:
                vals = [int(x) for x in str(saved).split(",") if x.strip()]
                if len(vals) == 2 and all(v >= 20 for v in vals):
                    return vals
        except Exception:
            pass
        return list(DEFAULT_BOTTOM_SPLIT_SIZES)

    def _save_bottom_split_sizes(self):
        """只在退出时落盘。

        不能接 ``splitterMoved``: 窗口第一次 layout / 每次缩放都会触发它,
        会把"还没被用户拖过"的中间值写进设置里, 下次启动就歪了。
        """
        try:
            w = self.bottom_split.sizes()
            if len(w) == 2 and all(v >= 20 for v in w):
                self._user_settings().setValue(
                    "ui/bottom_split", f"{int(w[0])},{int(w[1])}")
        except Exception:
            pass

    # ------------------------------------------------- 视频列表列宽自适应
    def _fit_tree_columns(self):
        """按列表可视宽度收起次要列。

        窗口默认只有 650 宽(左列约 290), 五列全展开时被 ResizeToContents 的
        四列一挤,「标题」只剩十几像素, 显示成「测试…」。宽度不够就先收起
        「码率」与「时长」(数据仍在条目的 tooltip 里), 把宽度让给标题;
        窗口拉宽后自动恢复。
        """
        try:
            w = self.tree.viewport().width()
        except Exception:
            return
        self.tree.setColumnHidden(3, w < 320)     # 时长
        self.tree.setColumnHidden(2, w < 430)     # 码率

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_tree_columns()

    def showEvent(self, event):
        super().showEvent(event)
        # 竖向分割的初始高度必须在**窗口完成第一次布局之后**再设。
        # 构造期 setSizes 时 splitter 还是 0 高, 各控件的 sizeHint 会把默认
        # 权重顶偏(实测想要的 1:1 变成了 147:84)。排队到事件循环里补一次即可。
        if not self._bottom_split_ready:
            self._bottom_split_ready = True
            QTimer.singleShot(0, self._apply_bottom_split_sizes)

    def _apply_bottom_split_sizes(self, attempt: int = 0):
        """按权重把当前可用高度算成像素再 setSizes。

        只喂 ``[1, 1]`` 给 QSplitter 是不够的: 它分配"多余空间"时会把两边的
        **最小高度**(任务区 84 / 日志 78)也算进权重, 于是 1:1 变成了 147:84。
        自己按高度换算成像素就没有这个歧义。

        ``showEvent`` 里排队的回调跑得比第一次布局还早(此时 splitter 高度仍是
        0), 所以要重试几次, 直到拿到真实高度为止。
        """
        try:
            sp = self.bottom_split
            avail = sp.height() - sp.handleWidth()
            if avail <= 8:
                if attempt < 25:
                    QTimer.singleShot(
                        40, lambda: self._apply_bottom_split_sizes(attempt + 1))
                return
            vals = self._bottom_split_sizes()
            total = float(sum(vals)) or 1.0
            sizes = [max(1, int(avail * v / total)) for v in vals]
            sizes[-1] = max(1, avail - sum(sizes[:-1]))
            sp.setSizes(sizes)
        except Exception:
            pass

    def _build_log_area(self) -> QWidget:
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setMinimumHeight(48)
        return self.log_view

    # ========================================== 深度搜索 / 缓存捕捉 / 更多功能
    def _install_profile_script(self, name: str, source: str,
                                sub_frames: bool = True) -> bool:
        """往默认 profile 挂一个 DocumentCreation 脚本(同名先移除, 可重复安装)。"""
        from PySide6.QtWebEngineCore import QWebEngineScript

        try:
            script = QWebEngineScript()
            script.setName(name)
            script.setSourceCode(source)
            script.setInjectionPoint(
                QWebEngineScript.InjectionPoint.DocumentCreation)
            script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            script.setRunsOnSubFrames(sub_frames)
            scripts = self._profile.scripts()
            for old in list(scripts.toList()):
                if old.name() == name:
                    scripts.remove(old)
            scripts.insert(script)
            return True
        except Exception as e:
            self._log_ui(f"注入脚本失败({name}): {e}")
            return False

    def _remove_profile_script(self, name: str) -> bool:
        """移除已挂的 profile 脚本(开关类能力关掉时用)。"""
        if self._profile is None:
            return False
        try:
            scripts = self._profile.scripts()
            for old in list(scripts.toList()):
                if old.name() == name:
                    scripts.remove(old)
            return True
        except Exception:
            return False

    def _setup_deep_and_capture(self):
        """装页面脚本 + 数据桥。

        必须在任何播放器创建 SourceBuffer 之前注入，否则
        ``MediaSource.prototype.addSourceBuffer`` 的代理挂不上。
        """
        page = self.web.page()
        self._bridge = PageBridge(self._on_cap_chunk, self._on_cap_meta,
                                  self._on_rec_data)
        ok_bridge = self._bridge.install(page)
        if ok_bridge:
            self._install_profile_script(
                "m3u8dl-bridge", self._bridge.inject_script(), False)
        ok_deep = self._install_profile_script(
            deepsearch.SCRIPT_NAME, deepsearch.build_script())
        ok_cap = self._install_profile_script(
            capture.SCRIPT_NAME, capture.build_script(self._cap_sid))
        ok_rec = self._install_profile_script(
            recorder.SCRIPT_NAME, recorder.build_script(self._cap_sid))
        # 广告拦截的元素层(CSS + 动态清理); 网络层的 block 在拦截器里做
        ok_adb = self._install_profile_script(
            adblock.SCRIPT_NAME, adblock.build_script())
        self._log_ui(
            "进阶能力: 深度搜索" + ("✓" if ok_deep else "✗")
            + " · 缓存捕捉" + ("✓" if (ok_cap and ok_bridge) else "✗")
            + " · 更多功能" + ("✓" if (ok_rec and ok_bridge) else "✗")
            + " · 广告拦截" + ("✓" if ok_adb else "✗")
            + "（页面每次加载自动重新挂钩）")
        # 页面跳转后注入脚本会重建，捕捉开关需要重新下发
        page.loadFinished.connect(self._reassert_capture)
        # 屏幕捕捉: getDisplayMedia 需要 Qt 侧代为选择画面源
        self._desktop_ua = (self._profile.httpUserAgent() if self._profile else "")
        try:
            page.desktopMediaRequested.connect(self._on_desktop_media_requested)
        except Exception:
            pass

    def _reassert_capture(self, ok: bool):
        if not ok or not WEBENGINE_OK or not self._cap_on:
            return
        try:
            self.web.page().runJavaScript(capture.ENABLE_JS)
        except Exception:
            pass

    # ---------------------------------------------------------------- 缓存捕捉
    def _on_cap_chunk(self, sid: str, mime: str, index: int, data: bytes):
        """页面回传一个媒体数据块(已在页面侧 base64 解码)。"""
        if not self._cap_on or not data:
            return
        try:
            self._capture.add_chunk(sid or self._cap_sid, mime, index, data)
        except Exception as e:
            self._log_ui(f"缓存捕捉写入失败: {e}")

    def _on_cap_meta(self, sid: str, key: str, value: str):
        if key == "__log__":
            return
        self._capture.add_meta(sid, key, value)
        if key == "done":
            self._log_ui("缓存捕捉: 播放器已送完数据(endOfStream), 可点击「保存」写盘")

    def _poll_capture_stats(self):
        if not WEBENGINE_OK:
            return

        def on_res(raw):
            import json

            try:
                st = json.loads(raw) if isinstance(raw, str) and raw else {}
            except Exception:
                st = {}
            if not isinstance(st, dict):
                st = {}
            self._cap_stats = st
            if not self._cap_on:
                return
            got = self._capture.total_bytes
            n = self._capture.chunk_count()
            streams = len(self._capture.streams())
            if not st.get("ready"):
                self.lbl_adv.setText("缓存捕捉: 数据桥未就绪, 请刷新页面后重试")
            elif got:
                tail = " · 播放器已结束" if st.get("done") else ""
                self.lbl_adv.setText(
                    f"缓存捕捉中 · {format_size(got)} / {n} 块 / {streams} 路" + tail)
            else:
                extra = (f" · 已建 {st.get('streams', 0)} 路 SourceBuffer"
                         if st.get("streams") else "")
                self.lbl_adv.setText(
                    "缓存捕捉中 · 等待数据: 请在页面里点击播放并让视频缓冲一段" + extra)
            self.btn_cap_save.setVisible(bool(got))

        try:
            self.web.page().runJavaScript(capture.STATS_JS, 0, on_res)
        except Exception:
            pass

    def _on_capture_toggle(self, on: bool):
        if not WEBENGINE_OK:
            self.btn_cap.setChecked(False)
            QMessageBox.information(self, "提示", "当前环境不支持浏览器引擎。")
            return
        if on:
            self._capture.clear()
            self._cap_on = True
            try:
                self.web.page().runJavaScript(capture.ENABLE_JS)
            except Exception:
                pass
            self.btn_cap.setText("■ 停止捕捉")
            self.tabs.setCurrentIndex(1)
            self._cap_timer.start()
            self.lbl_adv.setText("缓存捕捉已开启: 请在页面点击播放, 让视频缓冲一段")
            self._log_ui("缓存捕捉已开启: 代理 MediaSource.appendBuffer, "
                         "留存播放器送进解码器的已解密裸字节")
            self._log_ui("提示: 播放进度走到哪里就能捕到哪里, 拖动进度条可扩大范围")
            url = self.web.url().toString()
            if not url or url == "about:blank":
                self._log_ui("当前浏览器页签为空, 请先在网址栏解析网页")
            return
        self._cap_on = False
        try:
            self.web.page().runJavaScript(capture.DISABLE_JS)
        except Exception:
            pass
        self._cap_timer.stop()
        self.btn_cap.setText("缓存捕捉")
        got = self._capture.total_bytes
        if got:
            self.lbl_adv.setText(
                f"缓存捕捉已停止 · 已留存 {format_size(got)} · 点击「保存」写盘")
            self.btn_cap_save.setVisible(True)
        else:
            self.lbl_adv.setText("缓存捕捉已停止 · 本次未捕获到数据")
            self.btn_cap_save.setVisible(False)
        self._log_ui(f"缓存捕捉已停止, 已留存 {format_size(got)}")

    def _save_capture(self):
        if self._capture.is_empty():
            QMessageBox.information(self, "提示", "还没有捕获到任何数据。")
            return
        workdir = self.ed_workdir.text().strip()
        if not workdir:
            QMessageBox.warning(self, "提示", "请先设置工作目录")
            return
        base = sanitize_name(self.ed_save.text().strip() or "capture")
        out_dir = Path(workdir)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            files = self._capture.save(out_dir, base, on_log=self._log_ui)
        finally:
            QApplication.restoreOverrideCursor()
        if not files:
            QMessageBox.warning(self, "保存失败", "写入失败, 请查看日志。")
            return
        final = files[0]
        muxed = None
        if len(files) > 1:
            muxed = mux_with_ffmpeg(files, out_dir / f"{base}.mp4",
                                    self.ffmpeg_path, self._log_ui)
            if muxed:
                final = muxed
        self._log_ui(f"缓存捕捉已保存: {final}")
        msg = "已保存 %d 路数据:\n%s" % (
            len(files), "\n".join(str(f) for f in files))
        if muxed:
            msg += f"\n\n音视频已合成: {muxed.name}"
        msg += "\n\n是否清空捕捉缓冲?"
        ret = QMessageBox.question(self, "保存完成", msg,
                                   QMessageBox.Yes | QMessageBox.No,
                                   QMessageBox.No)
        if ret == QMessageBox.Yes:
            self._capture.clear()
            self.btn_cap_save.setVisible(False)
            self.lbl_adv.setText("捕捉缓冲已清空")

    # ---------------------------------------------------------------- 深度搜索
    def _on_deep_search(self):
        if not WEBENGINE_OK or self._profile is None:
            QMessageBox.information(self, "提示", "当前环境不支持浏览器引擎。")
            return
        url = self.web.url().toString()
        if not url or url == "about:blank":
            target = self.ed_url.text().strip()
            if not target:
                QMessageBox.information(
                    self, "提示",
                    "请先在上方输入网址并「解析网页」,\n"
                    "或直接在「浏览器嗅探」页签打开目标页面。")
                return
            if not target.startswith(("http://", "https://")):
                target = "https://" + target
            self.tabs.setCurrentIndex(1)
            self._start_web_sniff(target)
            QMessageBox.information(
                self, "深度搜索",
                "页面正在加载, 加载完成(可点击播放)后再点一次「深度搜索」。")
            return
        self.btn_deep.setEnabled(False)
        self.lbl_adv.setText("深度搜索中: 正在翻查页面内存…")
        self._log_ui("深度搜索: 读取页面内存中的地址 / m3u8 原文 / 疑似密钥…")
        state = {"done": False}

        def on_res(raw):
            if state["done"]:
                return
            state["done"] = True
            self.btn_deep.setEnabled(True)
            self._apply_deep_result(raw)

        try:
            self.web.page().runJavaScript(deepsearch.READ_JS, 0, on_res)
        except Exception as e:
            self.btn_deep.setEnabled(True)
            self._log_ui(f"深度搜索失败: {e}")
            return
        # 兜底: 回调丢失时不至于按钮永久置灰
        QTimer.singleShot(2500, lambda: on_res(""))

    def _deep_text_to_infos(self, text: str, base_url: str, seq: int) -> list:
        """把捕获到的 m3u8 原文变成可下载条目(不需要该地址真的存在)。"""
        out = []
        if not text:
            return out
        try:
            pl = parse_m3u8(text, base_url)
        except Exception:
            return out
        if isinstance(pl, MasterPlaylist):
            for v in pl.variants:
                info = VideoInfo(
                    title=f"深度搜索#{seq} 内存清单",
                    mime="application/vnd.apple.mpegurl",
                    resolution=v.resolution,
                    bitrate_kbps=v.bitrate_kbps,
                    url=v.url,
                    source=base_url,
                    headers=self._page_headers(base_url),
                    variant_count=len(pl.variants))
                out.append(info)
            return out
        if not isinstance(pl, MediaPlaylist) or not pl.segments:
            return out
        dur = pl.duration or sum(s.duration for s in pl.segments)
        info = VideoInfo(
            title=f"深度搜索#{seq} 内存清单",
            mime="application/vnd.apple.mpegurl",
            duration_text=format_duration(dur) if dur else "",
            size_text=f"{len(pl.segments)} 分片",
            url=base_url,
            is_live=pl.is_live,
            source=base_url,
            headers=self._page_headers(base_url))
        # 挂上原文: 下载器据此跳过网络请求(该地址可能根本不存在)
        info.playlist_text = text      # type: ignore[attr-defined]
        out.append(info)
        return out

    def _append_info(self, info: VideoInfo):
        self.infos.append(info)
        item = QTreeWidgetItem([
            info.display_title,
            info.resolution or "—",
            f"{info.bitrate_kbps}kbps" if info.bitrate_kbps else "—",
            info.duration_text or "—",
            info.size_text or "—",
        ])
        item.setData(0, Qt.UserRole, info)
        item.setToolTip(0, info.url)
        self.tree.addTopLevelItem(item)
        if self.tree.currentItem() is None:
            self.tree.setCurrentItem(item)

    def _apply_deep_result(self, raw: str):
        res = deepsearch.parse_result(raw)
        urls, texts, keys = res["urls"], res["texts"], res["keys"]
        self._deep_keys = list(keys or [])
        self._deep_texts = list(texts or [])
        kc = deepsearch.key_candidates(self._deep_keys)
        self._log_ui(f"深度搜索: 地址 {len(urls)} 个 · m3u8 原文 {len(texts)} 段 · "
                     f"疑似密钥 {len(kc)} 个")
        page_url = self.web.url().toString() or self.ed_url.text().strip()

        added = 0
        for n, text in enumerate(texts, start=1):
            for info in self._deep_text_to_infos(text, page_url, n):
                self._append_info(info)
                added += 1

        cand = [u for u in urls
                if re.search(r"\.(m3u8|m3u|mpd|mp4|flv)(\?|#|$)", u, re.I)]
        if cand:
            defaults = self._page_headers(page_url)
            headers_map = {u: dict(defaults) for u in cand}
            if self._cookies is not None:
                for u in cand:
                    ck = self._cookies.header_for(u)
                    if ck:
                        headers_map[u]["Cookie"] = ck
            self._log_ui(f"深度搜索: 解析 {len(cand)} 个候选地址…")
            self.sniffer_worker = SnifferWorker(
                page_url, use_proxy=not self.ck_noproxy.isChecked(),
                candidate_urls=cand,
                page_title=self._web_sniff_title or "深度搜索",
                headers_map=headers_map)
            self.sniffer_worker.finished.connect(self._on_deep_urls_done)
            self.sniffer_worker.failed.connect(self._on_deep_urls_failed)
            self.sniffer_worker.log.connect(self._log_ui)
            self.sniffer_worker.start()

        if not added and not cand:
            self.lbl_adv.setText("深度搜索: 未捞到可用内容(可播放后再搜一次)")
            QMessageBox.information(
                self, "深度搜索",
                "未从页面内存中捞到 m3u8 地址、原文或密钥。\n\n"
                "建议: 在页面里点击播放, 让播放器真正开始工作, "
                "再点一次「深度搜索」。")
            return
        self.lbl_adv.setText(
            f"深度搜索完成: 新增 {added} 条内存清单 · 候选地址 {len(cand)} 个 · "
            f"疑似密钥 {len(kc)} 个")
        if kc:
            brief = ", ".join(fragtools.key_to_hex(k)[:8] + "…" for k in kc[:4])
            self._log_ui(f"疑似密钥(前 4): {brief} —— 可在「更多功能」一键验证真伪")
        if added:
            self._auto_download_best()

    def _on_deep_urls_done(self, infos):
        n = 0
        for info in infos or []:
            self._append_info(info)
            n += 1
        self._log_ui(f"深度搜索地址解析完成, 新增 {n} 条")
        self._auto_download_best()

    def _on_deep_urls_failed(self, msg: str):
        """深度搜索的候选地址解析失败: 只记日志, 不弹模态框打断流程。"""
        self._log_ui(f"深度搜索: 候选地址解析失败 · {msg}")
        self.lbl_adv.setText(
            "深度搜索: 内存清单已入列表, 候选地址解析失败(可先下载内存清单)")

    # ---------------------------------------------------------------- 更多功能
    def _load_playlist_for_info(self, info) -> Optional[MediaPlaylist]:
        """取 info 对应的媒体列表: 优先用捕获原文, 否则联网拉取。"""
        if info is None:
            return None
        import requests

        text = getattr(info, "playlist_text", "") or ""
        base = info.source or info.url
        if not text:
            try:
                r = requests.get(info.url, headers=info.headers or {}, timeout=15)
                r.raise_for_status()
                text, base = r.text, info.url
            except Exception as e:
                self._log_ui(f"读取播放列表失败: {e}")
                return None
        try:
            pl = parse_m3u8(text, base)
        except Exception as e:
            self._log_ui(f"播放列表解析失败: {e}")
            return None
        if isinstance(pl, MasterPlaylist):
            if not pl.variants:
                return None
            v = max(pl.variants, key=lambda x: x.bandwidth)
            try:
                r = requests.get(v.url, headers=info.headers or {}, timeout=15)
                r.raise_for_status()
                pl = parse_m3u8(r.text, v.url)
            except Exception as e:
                self._log_ui(f"读取清晰度变体失败: {e}")
                return None
        return pl if isinstance(pl, MediaPlaylist) else None

    def _fetch_fragments(self) -> List[str]:
        if self.current_info is None:
            return []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            pl = self._load_playlist_for_info(self.current_info)
        finally:
            QApplication.restoreOverrideCursor()
        if pl is None:
            return []
        return [s.url for s in pl.segments]

    def _brute_key(self, candidates):
        """用首个分片验证哪个疑似 KEY 是真密钥。"""
        info = self.current_info
        if info is None or not candidates:
            return None
        import requests

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            pl = self._load_playlist_for_info(info)
            if pl is None or not pl.segments:
                return None
            seg = pl.segments[0]
            r = requests.get(seg.url, headers=info.headers or {}, timeout=20)
            r.raise_for_status()
            iv_hex = ""
            if pl.key and pl.key.iv:
                iv_hex = pl.key.iv.replace("0x", "").replace("0X", "")
            iv = (bytes.fromhex(iv_hex) if len(iv_hex) == 32
                  else (pl.media_sequence + 0).to_bytes(16, "big"))
            return fragtools.brute_force_key(r.content, candidates, iv)
        except Exception as e:
            self._log_ui(f"密钥验证失败: {e}")
            return None
        finally:
            QApplication.restoreOverrideCursor()

    def _on_tools(self):
        kc = deepsearch.key_candidates(self._deep_keys)
        dlg = ToolsDialog(
            self, state=self.adv, info=self.current_info, on_log=self._log_ui,
            fetch_fragments=self._fetch_fragments,
            key_candidates=kc, brute_force=self._brute_key,
            hooks=self._tools_hooks())
        accepted = dlg.exec() == QDialog.Accepted
        st = dlg.result_state()
        if accepted:
            self.adv = st
        else:
            # 录制 / 模拟手机 / 自动下载 / 数据发送 是**立即生效的动作**，
            # 点取消不会回滚它们（页面已经重载、文件已经录完）。这里只把这几个
            # 字段同步回来，避免界面状态与实际状态不一致；分片筛选等纯设置项
            # 保持点开面板前的值。
            for k in ("rec_mime", "rec_index", "simulate_mobile",
                      "auto_download", "adblock", "send_url"):
                if k in st:
                    self.adv[k] = st[k]
            self.auto_download = bool(self.adv.get("auto_download"))
            return
        self.auto_download = bool(self.adv.get("auto_download"))
        bits = []
        if self.adv.get("selected") is not None:
            bits.append(f"只下 {len(self.adv['selected'])} 个分片")
        if self.adv.get("strip_image"):
            bits.append("剥离伪装头")
        if self.adv.get("skip_decrypt"):
            bits.append("跳过解密")
        if self.adv.get("audio_only"):
            bits.append("仅音频")
        if self.adv.get("extra_query"):
            bits.append("追加分片参数")
        if self.adv.get("simulate_mobile"):
            bits.append("模拟手机")
        if self.adv.get("auto_download"):
            bits.append("自动下载")
        if self.adv.get("send_url"):
            bits.append("已配置数据发送")
        self._log_ui("更多功能已更新: "
                     + ("、".join(bits) if bits else "全部默认(无额外处理)"))
        if bits:
            self.lbl_adv.setText("更多功能生效: " + "、".join(bits))

    # ============================================ 录制 / 自动化（猫抓 6 按钮）
    def _on_desktop_media_requested(self, request):
        """getDisplayMedia 授权。

        Qt 不像 Chrome 会弹系统画面源选择器，只能由宿主替用户选。这里取第一个
        屏幕；拿不到就取消（页面侧会收到 NotAllowedError，正常降级）。
        """
        try:
            model = request.screensModel()
            if model is not None and model.rowCount() > 0:
                request.selectScreen(model.index(0, 0))
                self._log_ui("屏幕捕捉: 已选择首个屏幕作为画面源")
                return
        except Exception:
            pass
        try:
            request.cancel()
            self._log_ui("屏幕捕捉: 没有可用画面源, 已取消")
        except Exception:
            pass

    def _on_rec_data(self, sid: str, kind: str, data: bytes):
        """页面回传的录制分片（单条连续流，直接 append）。"""
        if data:
            self._rec.add(data)

    def _rec_sources(self):
        state = {"done": False, "val": []}

        def cb(raw):
            state["done"] = True
            state["val"] = recorder.parse_sources(raw)

        self._run_js_sync(recorder.SOURCES_JS, cb, state, timeout=1.0)
        return True, state["val"]

    def _rec_mimes(self):
        state = {"done": False, "val": []}

        def cb(raw):
            state["done"] = True
            state["val"] = recorder.parse_mimes(raw)

        self._run_js_sync(recorder.MIMES_JS, cb, state, timeout=1.0)
        return True, state["val"]

    def _rec_stats(self):
        state = {"done": False, "val": {}}

        def cb(raw):
            state["done"] = True
            state["val"] = recorder.parse_stats(raw)

        self._run_js_sync(recorder.STATS_JS, cb, state, timeout=1.0)
        return True, state["val"]

    def _run_js_sync(self, js: str, cb, state: dict, timeout: float = 1.0):
        """同步执行页面 JS 并等回调（带超时）。

        `runJavaScript` 的回调是异步投递的，这里用短事件循环把它等回来，只用于
        用户点按钮这类可忍受 ~1s 延时的操作。
        """
        if not WEBENGINE_OK:
            return
        try:
            self.web.page().runJavaScript(js, 0, cb)
        except Exception:
            return
        deadline = time.monotonic() + max(0.1, timeout)
        while not state.get("done") and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)

    def _rec_toggle(self, kind: str, index: int = 0, mime: str = ""):
        if not WEBENGINE_OK:
            return False, "当前环境不支持浏览器引擎"
        if self._rec_kind:
            return False, "已经在录制中, 请先停止"
        url = self.web.url().toString()
        if not url or url == "about:blank":
            return False, "请先在「浏览器嗅探」页签打开目标页面"
        self._rec.abort()
        self._rec.begin(kind)
        self._rec_mime = mime or ""
        state = {"done": False, "val": ""}

        def cb(res):
            state["done"] = True
            state["val"] = str(res or "")

        try:
            self.web.page().runJavaScript(
                recorder.start_js(kind, index, mime), 0, cb)
        except Exception as e:
            self._rec.abort()
            return False, f"注入失败: {e}"
        # 屏幕捕捉要走 getDisplayMedia，是异步的，不能死等
        self._run_js_sync("1", lambda *_: None, {"done": True}, 0.1)
        deadline = time.monotonic() + (2.5 if kind == "screen" else 1.2)
        while not state["done"] and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
        res = state["val"]
        if res in ("started", "pending"):
            self._rec_kind = kind
            self._rec_timer.start()
            self._log_ui(f"录制已开始({kind})")
            return True, recorder.START_HINTS.get(res, res)
        self._rec.abort()
        self._rec_kind = ""
        err = recorder.START_HINTS.get(res, res or "页面没有响应")
        self._log_ui(f"录制启动失败: {err}")
        return False, err

    def _rec_stop(self):
        if not self._rec_kind:
            return False, "当前没有在录制"
        kind = self._rec_kind
        try:
            self.web.page().runJavaScript(recorder.STOP_JS)
        except Exception:
            pass
        # 最后一片数据是异步到的，等它落进 RecordStore 再写文件
        deadline = time.monotonic() + 0.9
        while time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.03)
        self._rec_timer.stop()
        self._rec_kind = ""
        workdir = self.ed_workdir.text().strip()
        if not workdir:
            self._rec.abort()
            return False, "未设置工作目录, 录制数据已丢弃"
        base = sanitize_name(self.ed_save.text().strip()
                             or f"录制_{time.strftime('%H%M%S')}")
        size = self._rec.bytes
        path = self._rec.finish(Path(workdir), base, self._rec_mime,
                                on_log=self._log_ui)
        if path is None:
            return False, f"没有录到数据({kind} 可能未产生可解码画面)"
        return True, f"已保存 {path.name} ({size / 1048576:.2f} MB)"

    def _poll_rec_stats(self):
        """录制中每秒刷新一次状态，异常时自动收尾。"""
        if not self._rec_kind:
            self._rec_timer.stop()
            return
        ok, st = self._rec_stats()
        if not ok or not isinstance(st, dict) or not st:
            return
        if st.get("state") == "error":
            err = st.get("error") or "录制中断"
            self._log_ui(f"录制异常: {err}")
            self._rec_timer.stop()
            self._rec_kind = ""
            self._rec.abort()

    def _toggle_mobile(self, on: bool):
        """模拟手机: 切 iPhone UA 并重载（对应猫抓 #simulateMobile）。"""
        if not WEBENGINE_OK or self._profile is None:
            return False, "当前环境不支持浏览器引擎"
        try:
            self._profile.setHttpUserAgent(MOBILE_UA if on
                                          else (self._desktop_ua or ""))
        except Exception as e:
            return False, f"切换 UA 失败: {e}"
        self._mobile_on = bool(on)
        try:
            self.web.reload()
        except Exception:
            pass
        return True, ("已切换为 iPhone UA 并重载页面" if on
                      else "已还原桌面 UA 并重载页面")

    def _toggle_auto_download(self, on: bool):
        self.auto_download = bool(on)
        return True, ("已开启: 解析结束后自动下载最佳一条" if on
                      else "已关闭自动下载")

    def _toggle_adblock(self, on: bool):
        """广告拦截开关。

        网络层(拦截器 ``block``)对**后续请求**立即生效; 元素层(CSS 隐藏)是挂在
        profile 上的 document-creation 脚本, 需要页面重新加载才会挂上/撤下。
        """
        if self._url_interceptor is None:
            return False, "当前环境不支持浏览器引擎"
        self.adblock_on = bool(on)
        self._url_interceptor.adblock = bool(on)
        if on:
            ok = self._install_profile_script(
                adblock.SCRIPT_NAME, adblock.build_script())
        else:
            ok = self._remove_profile_script(adblock.SCRIPT_NAME)
        tip = ("已开启: 拦截广告/统计请求并隐藏广告位(元素层下次加载生效)"
               if on else "已关闭广告拦截")
        return bool(ok), tip

    def _send_data(self, url: str):
        """数据发送: 把当前视频列表 POST 到外部服务（对应猫抓 send2local）。"""
        if not self.infos:
            return False, "列表为空, 没有可发送的数据"
        videos = []
        for i in self.infos:
            videos.append({
                "title": i.title, "url": i.url, "mime": i.mime,
                "resolution": i.resolution, "bitrate_kbps": i.bitrate_kbps,
                "duration": i.duration_text, "size": i.size_text,
                "live": i.is_live, "source": i.source,
                "headers": i.headers or {},
            })
        page_url = self.web.url().toString() or self.ed_url.text().strip()
        title = self._web_sniff_title or (self.infos[0].title if self.infos else "")
        payload = recorder.build_payload(title, page_url, videos)
        ok, msg = recorder.send_json(url, payload, timeout=8)
        self._log_ui(f"数据发送: {msg}")
        return ok, msg

    def _quality_score(self, info) -> int:
        """给自动下载挑最佳条目用：码率优先, 其次分辨率。"""
        score = int(info.bitrate_kbps or 0) * 1000
        m = re.search(r"(\d+)\s*[x×]\s*(\d+)", info.resolution or "")
        if m:
            try:
                score += int(m.group(1)) * int(m.group(2)) // 100
            except Exception:
                pass
        return score

    def _auto_download_best(self):
        """自动下载（对应猫抓 #autoDown）: 解析完成后把最佳一条加入队列。

        同一批解析结果只触发一次: 挑完最佳后把**本批所有地址**都记进
        ``_auto_done``，否则同一份列表被再次喂进来时，残余的低码率条目会被
        当成"新条目"再下一个任务。后到的更高码率地址仍会正常触发。
        """
        if not self.auto_download or not self.infos:
            return
        cand = [i for i in self.infos if i.url not in self._auto_done]
        if not cand:
            return
        if not self.ed_workdir.text().strip():
            self._log_ui("自动下载跳过: 未设置工作目录")
            return
        best = max(cand, key=self._quality_score)
        for i in cand:
            self._auto_done.add(i.url)
        if len(self._auto_done) > 2000:
            self._auto_done.clear()
            self._auto_done.add(best.url)
        for n in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(n)
            if it.data(0, Qt.UserRole) is best:
                self.tree.setCurrentItem(it)
                break
        self._log_ui(f"自动下载: {best.display_title}")
        self._on_download()

    def _tools_hooks(self) -> dict:
        return {
            "record": self._rec_toggle,
            "stop": self._rec_stop,
            "stats": self._rec_stats,
            "mimes": self._rec_mimes,
            "sources": self._rec_sources,
            "mobile": self._toggle_mobile,
            "auto": self._toggle_auto_download,
            "adblock": self._toggle_adblock,
            "send": self._send_data,
        }

    # ================================================================ 嗅探
    def _on_sniff(self):
        url = self.ed_url.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请输入网页地址")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.ed_url.setText(url)
        if self.sniffer_worker and self.sniffer_worker.isRunning():
            return
        if self._web_sniff is not None and self._web_sniff.running:
            return
        if self._cdp.active:
            # 解析网页会清空列表, 必须先停掉系统浏览器监听, 否则白抓
            self._stop_sys_browser()
            return
        self._web_sniff_used = False
        self.btn_sniff.setEnabled(False)
        self.btn_stop_sniff.setEnabled(True)
        self.tree.clear()
        self.infos = []
        self.current_info = None
        self._log_ui(f"开始嗅探: {url}")
        from .. import bili as bili_mod

        if bili_mod.is_bili_url(url):
            self._start_bili(url)
            return
        self.sniffer_worker = SnifferWorker(url, use_proxy=not self.ck_noproxy.isChecked())
        self.sniffer_worker.finished.connect(self._on_sniff_done)
        self.sniffer_worker.failed.connect(self._on_sniff_fail)
        self.sniffer_worker.log.connect(self._log_ui)
        self.sniffer_worker.start()

    def _on_stop_sniff(self):
        if self._cdp.active:
            # 系统浏览器监听中: 结束监听并解析已捕获的地址
            self._stop_sys_browser()
            return
        if self._web_sniff is not None and self._web_sniff.running:
            # 浏览器监听中: 立即结束监听并解析已捕获的地址
            self._log_ui("手动结束浏览器监听…")
            self._web_sniff.stop()
            return
        if self.bili_worker and self.bili_worker.isRunning():
            self.bili_worker.terminate()
            self.bili_worker.wait(1000)
            self._log_ui("B站 解析已停止")
        if self.sniffer_worker and self.sniffer_worker.isRunning():
            self.sniffer_worker.terminate()
            self.sniffer_worker.wait(1000)
            self._log_ui("嗅探已停止")
        self._sniff_ui_reset()

    def _sniff_ui_reset(self):
        self.btn_sniff.setEnabled(True)
        self.btn_stop_sniff.setEnabled(False)
        if self.btn_sys_browser.isChecked():
            # 阻塞信号: 否则会回头再走一遍 _stop_sys_browser
            self.btn_sys_browser.blockSignals(True)
            self.btn_sys_browser.setChecked(False)
            self.btn_sys_browser.blockSignals(False)

    def _on_sniff_done(self, infos):
        self._sniff_ui_reset()
        self.infos = infos
        if not infos:
            # 静态解析无结果: 若为网页地址且尚未回退过, 自动切换浏览器引擎
            # 监听动态加载的视频(JS/XHR 延迟请求的 m3u8 不在 HTML 源码中)
            url = self.ed_url.text().strip()
            if (not self._web_sniff_used and self._web_sniff is not None
                    and self._is_page_url(url)):
                self._start_web_sniff(url)
                return
            self._log_ui("未发现可下载的视频地址")
            QMessageBox.information(
                self, "嗅探完成",
                "未在该页面发现 m3u8 / mp4 视频地址。\n\n"
                "若确实有视频: 部分站点的清单在 token 过期或缺少 Cookie 时会被\n"
                "重定向到占位流(被判定为广告流而丢弃)。请在「浏览器嗅探」页签\n"
                "里点击播放, 让播放器重新请求一次, 再点「解析网页」。")
            return
        self._fill_results(infos)

    def _fill_results(self, infos) -> None:
        """把解析结果填进左侧列表(m3u8 嗅探与 B站 解析共用)。"""
        for info in infos:
            item = QTreeWidgetItem([
                info.display_title,
                info.resolution or "—",
                f"{info.bitrate_kbps}kbps" if info.bitrate_kbps else "—",
                info.duration_text or "—",
                info.size_text or "—",
            ])
            item.setData(0, Qt.UserRole, info)
            # 窄窗下「码率」「时长」列会被收起, 完整信息一律挂在 tooltip 上
            tip = [
                info.display_title or "",
                f"清晰度: {info.resolution or '—'}    "
                f"码率: {info.bitrate_kbps or '—'} kbps",
                f"时长: {info.duration_text or '—'}    "
                f"大小: {info.size_text or '—'}",
                info.url or "",
            ]
            if getattr(info, "audio_url", ""):
                tip.insert(3, "音轨: 独立 DASH 流(下载后自动合并为 mp4)")
            item.setToolTip(0, "\n".join(tip))
            self.tree.addTopLevelItem(item)
        self._fit_tree_columns()
        self._log_ui(f"嗅探完成, 共发现 {len(infos)} 个视频")
        self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._auto_download_best()

    # ------------------------------------------------------------------ B站
    def _start_bili(self, url: str) -> None:
        """B站专用通路: 官方 playurl 接口 + DASH 音视频自动合并。

        B站不适合走网页嗅探: ① 视频/音频是两条独立的 m4s 流; ② 页面里的
        播放地址由 JS 动态取, 抓请求时音轨常常还没发。直接按视频页里的
        BV 号调接口最稳(见 m3u8dl/bili.py)。
        """
        cookie = ""
        if self._cookies is not None:
            try:
                # 内置浏览器里登录过 B站 就带上登录态, 否则最高只有 480P
                cookie = self._cookies.header_for("https://www.bilibili.com/")
            except Exception:
                cookie = ""
        self._log_ui("识别为 B站 视频页, 走 DASH 专用解析(音视频自动合并)")
        if cookie:
            self._log_ui("已带上内置浏览器的 B站 Cookie(可解锁更高画质)")
        self.bili_worker = BiliWorker(
            url, cookie=cookie, use_proxy=not self.ck_noproxy.isChecked())
        self.bili_worker.log.connect(self._log_ui)
        self.bili_worker.finished.connect(self._on_bili_done)
        self.bili_worker.failed.connect(self._on_bili_fail)
        self.bili_worker.start()

    def _on_bili_done(self, result) -> None:
        from ..sniffer import VideoInfo

        self._sniff_ui_reset()
        infos = []
        page_url = (f"https://www.bilibili.com/video/{result.bvid}"
                    if result.bvid else "")
        for st in result.streams:
            infos.append(VideoInfo(
                title=result.display_title,
                mime="video/mp4",
                resolution=st.resolution,
                bitrate_kbps=st.bitrate_kbps,
                duration_text=st.duration_text,
                size_text=st.size_text,
                url=st.video_url,
                audio_url=st.audio_url,
                source=page_url,
                headers=dict(st.headers or {}),
            ))
        self.infos = infos
        if result.cookie_tip:
            self._log_ui(result.cookie_tip)
        self._log_ui(f"B站 解析完成: {result.display_title} · "
                     f"{len(infos)} 个清晰度可选")
        self._fill_results(infos)

    def _on_bili_fail(self, msg: str) -> None:
        self._sniff_ui_reset()
        self._log_ui(f"B站 解析失败: {msg}")
        QMessageBox.critical(
            self, "B站 解析失败",
            f"{msg}\n\n提示: B站 接口有风控, 若反复报 412, 请在「浏览器嗅探」"
            "页签打开一次 B站(登录后还能解锁 1080P+), 再回来解析。")

    def _on_sniff_fail(self, msg):
        self._sniff_ui_reset()
        self._log_ui(f"静态嗅探失败: {msg}")
        # 直链失败是真实错误; 网页地址则回退浏览器引擎重试(可能被反爬拦截)
        url = self.ed_url.text().strip()
        if (not self._web_sniff_used and self._web_sniff is not None
                and self._is_page_url(url)):
            self._log_ui("切换浏览器引擎重试…")
            self._start_web_sniff(url)
            return
        QMessageBox.critical(self, "嗅探失败", msg)

    # ================================================================ 浏览器引擎嗅探
    @staticmethod
    def _is_page_url(url: str) -> bool:
        """是否为网页地址(而非 m3u8/mp4/flv 视频直链)。"""
        try:
            path = urlparse(url).path.lower()
        except Exception:
            return False
        return url.startswith(("http://", "https://")) and not path.endswith(
            (".m3u8", ".mp4", ".flv"))

    def _start_web_sniff(self, url: str):
        """启动浏览器引擎监听: 页面在「浏览器嗅探」页签加载, 拦截其网络请求。"""
        self._web_sniff_used = True
        self._web_sniff_url = url
        self._web_sniff_title = ""
        self.btn_sniff.setEnabled(False)
        self.btn_stop_sniff.setEnabled(True)
        self.tabs.setCurrentIndex(1)
        self.lbl_web.setText(
            "浏览器嗅探中: 页面正在加载, 视频地址将由网络请求拦截 + 页面脚本"
            "钩子自动捕获。(内置浏览器不含 H.264 解码器, 页面内可能无法播放, "
            "但不影响嗅探与下载; 如页面需要点击才开始加载, 请点击播放按钮)")
        self._log_ui("静态解析未发现视频, 已切换浏览器引擎(复刻猫抓的网络请求拦截)")
        self._log_ui("页面在「浏览器嗅探」页签加载中… 可点击播放按钮触发视频加载")
        self._web_sniff.start()
        self.web.setUrl(QUrl(url))

    # ================================================================ 系统浏览器嗅探
    def _on_sys_browser_toggle(self, on: bool):
        """「系统浏览器」按钮: 按下=启动系统浏览器监听, 弹起=结束并解析。"""
        if on:
            self._start_sys_browser()
        else:
            self._stop_sys_browser()

    def _start_sys_browser(self):
        if self._cdp.active:
            return
        url = self.ed_url.text().strip()
        if not url:
            self.btn_sys_browser.setChecked(False)
            QMessageBox.warning(self, "提示", "请输入网页地址")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
            self.ed_url.setText(url)
        if self.sniffer_worker and self.sniffer_worker.isRunning():
            self.sniffer_worker.terminate()
            self.sniffer_worker.wait(800)
        # 内置引擎监听若在跑, 先让位(两套监听同时开会互相覆盖结果)
        if self._web_sniff is not None and self._web_sniff.running:
            self._web_sniff.stop()

        self._cdp_items = []
        self._cdp_url = url
        self._web_sniff_url = url
        self._web_sniff_title = ""
        self.tree.clear()
        self.infos = []
        self.current_info = None
        self.btn_sniff.setEnabled(False)
        self.btn_stop_sniff.setEnabled(True)
        self._log_ui(f"启动系统浏览器嗅探: {url}")
        if not self._cdp.start(url):
            self.btn_sys_browser.setChecked(False)
            self._sniff_ui_reset()
            QMessageBox.warning(
                self, "无法启动",
                "未找到 Edge / Chrome, 或启动失败。\n"
                "系统浏览器嗅探需要本机安装 Edge 或 Chrome。")

    def _stop_sys_browser(self):
        if self._cdp.active:
            self._log_ui("结束系统浏览器监听, 开始解析已捕获的地址…")
            self._cdp.stop(close_browser=False)

    def _on_cdp_hit(self, item: dict):
        url = item.get("url") or ""
        if not url:
            return
        self._cdp_items.append(item)
        ref = (item.get("headers") or {}).get("Referer") or ""
        self._log_ui(
            f"系统浏览器捕获[{item.get('type','')}]: {url[:100]}"
            + (f" · Referer: {ref[:60]}" if ref else ""))
        self.lbl_adv.setText(f"系统浏览器已捕获 {len(self._cdp_items)} 条候选")

    def _on_cdp_finished(self):
        if self._closing:
            return
        self._sniff_ui_reset()
        items = self._cdp_items
        if not items:
            self._log_ui("系统浏览器监听未捕获到视频地址")
            self._log_ui(
                "提示: 请在弹出的浏览器里点击播放, 视频请求发出后再结束监听")
            QMessageBox.information(
                self, "嗅探完成",
                "系统浏览器未捕获到可用的视频地址。\n\n"
                "· 请在打开的浏览器窗口里点击播放按钮\n"
                "· 等播放器真正开始加载(进度条有变化)后再点「系统浏览器」结束\n"
                "· 若页面有 Cloudflare 人机校验, 需要先在浏览器里通过校验")
            return
        self._finish_web_sniff(rank_candidate_items(items))

    def _on_web_hit(self, url: str, rtype: str):
        self._log_ui(f"捕获视频请求[{rtype}]: {url[:110]}")


    def _on_web_page_loaded(self, ok: bool):
        if not ok:
            self._log_ui("页面加载失败")
            return
        try:
            self._web_sniff_title = self.web.page().title() or ""
        except Exception:
            self._web_sniff_title = ""
        self._log_ui(f"页面加载完成: {self._web_sniff_title or '(无标题)'}")
        self._log_ui(
            f"继续监听 {WebSniffSession.SETTLE_MS // 1000} 秒, "
            "捕获延迟加载的视频地址…")

    def _on_web_sniff_finished(self, items: list):
        """网络请求监听结束: 先合并页面脚本钩子捕获的地址, 再解析。"""
        self._collect_page_urls(list(items or []))

    @staticmethod
    def _normalize_items(items) -> list:
        out = []
        for it in items or []:
            if isinstance(it, str):
                out.append({"url": it, "headers": {}, "referer": ""})
            elif isinstance(it, dict) and it.get("url"):
                out.append(it)
        return out

    def _collect_page_urls(self, base_items: list):
        """读取页面脚本收集的地址(window.__M3U8DL_URLS)并与拦截结果合并。"""
        base = self._normalize_items(base_items)
        state = {"done": False}

        def proceed(merged):
            if state["done"]:
                return
            state["done"] = True
            self._finish_web_sniff(merged)

        def on_js(res):
            extra = []
            try:
                import json as _json
                val = _json.loads(res) if isinstance(res, str) and res else []
                if isinstance(val, list):
                    extra = [u for u in val if isinstance(u, str)]
            except Exception:
                extra = []
            if extra:
                self._log_ui(f"页面脚本钩子额外捕获 {len(extra)} 个请求地址")
            merged = base + [
                {"url": u, "headers": {}, "referer": ""} for u in extra]
            proceed(rank_candidate_items(merged))

        try:
            self.web.page().runJavaScript(READ_PAGE_URLS_JS, 0, on_js)
        except Exception:
            proceed(rank_candidate_items(base))
        # 兜底: 回调丢失或页面已卸载时不至于卡住界面
        QTimer.singleShot(1500, lambda: proceed(rank_candidate_items(base)))

    # -------------------------------------------------------- 请求头重组
    def _page_headers(self, page_url: str) -> dict:
        """页面级默认请求头: 与内置浏览器保持一致, 避免被 CDN 指纹识别。"""
        h = {}
        try:
            h["User-Agent"] = QWebEngineProfile.defaultProfile().httpUserAgent()
        except Exception:
            pass
        if page_url:
            h["Referer"] = page_url
            try:
                p = urlparse(page_url)
                if p.scheme and p.netloc:
                    h["Origin"] = f"{p.scheme}://{p.netloc}"
            except Exception:
                pass
        h["Accept"] = "*/*"
        h["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
        return h

    # 允许从浏览器请求中回放的请求头(其余如 Content-Length/Range 不转发)
    _REPLAY_HEADERS = ("referer", "origin", "user-agent", "accept",
                       "accept-language", "authorization", "cookie",
                       "x-requested-with")

    def _build_headers(self, item: dict, defaults: dict) -> dict:
        """浏览器实际请求头 + 同域 Cookie -> 可用于解析/下载的请求头。

        带防盗链的 CDN 会校验 Referer/Origin/UA/Cookie 组合, 用通用请求头二次
        请求必然被拒, 必须原样回放。

        **Referer 的优先级是这里的关键**(v1.1.9 根因): 浏览器真实发出的
        ``Referer`` 优先; ``firstPartyUrl`` 只在捕获头里没有 Referer 时才兜底。
        反例: uqload 的播放器在 ``uqload.vc`` 域内发 XHR, 真实 Referer 是
        ``https://uqload.vc/``; 若用宿主页(``livecamrips.to``)覆盖它, 站点
        会 302 到 ``/taunt/promo/master.m3u8`` 占位流 —— 表现为嗅探为 0、
        预览 Could not open file、下载报"占位流"。

        **不能用 defaults 打底**(v1.1.10 根因): 部分 CDN 做的是**请求头集合
        指纹**校验, 多一个它没见过的头就直接 403 —— ``streambucket.xyz`` 只要
        请求里出现 ``Accept-Language``(无论 zh-CN 还是 en-US)就拒, 而这条头是
        我们 defaults 里写死的, 浏览器那条 XHR 根本就没发。所以: **捕获头存在
        时它就是唯一来源**, defaults 仅用于补 UA(极少数拦截器不给)与
        捕获头为空时的完整兜底。
        """
        captured = dict(item.get("headers") or {})
        h: dict = {}
        if not captured:
            # 没有捕获头(如直接在网址栏粘贴 m3u8): 只能用默认头
            h.update(defaults)

        for k, v in captured.items():
            lk = k.lower()
            if (lk in self._REPLAY_HEADERS or lk.startswith("x-")
                    or lk.startswith("sec-ch-ua")):
                h[k] = v

        # UA 兜底: 极少数情况下拦截器不报 UA
        if not any(k.lower() == "user-agent" for k in h):
            ua = (defaults or {}).get("User-Agent") or ""
            if ua:
                h["User-Agent"] = ua

        # 捕获头里到底有没有 Referer(大小写不敏感)
        has_ref = any(k.lower() == "referer" for k in captured)
        if not has_ref:
            ref = item.get("referer") or ""
            if ref:
                h["Referer"] = ref
                try:
                    p = urlparse(ref)
                    if p.scheme and p.netloc:
                        h.setdefault("Origin", f"{p.scheme}://{p.netloc}")
                except Exception:
                    pass
        # Cookie: 系统浏览器(CDP)捕获的头里已经带真实 Cookie, 那是唯一有效
        # 的来源(内置引擎的 Cookie 罐里根本没有那个站的登录态); 只有捕获头
        # 里没有 Cookie 时, 才用内置浏览器的 Cookie 罐兜底。
        if not any(k.lower() == "cookie" for k in h):
            if self._cookies is not None:
                ck = self._cookies.header_for(item.get("url", ""))
                if ck:
                    h["Cookie"] = ck
        return {k: v for k, v in h.items() if v}

    def _finish_web_sniff(self, items: list):
        self._sniff_ui_reset()
        urls = [it["url"] for it in items]
        if not urls:
            self._log_ui("浏览器监听结束: 未捕获到视频地址")
            self._log_ui(
                "提示: 若页面需点击播放才加载视频, 请播放后再次点击「解析网页」")
            QMessageBox.information(
                self, "嗅探完成",
                "浏览器监听未捕获到可用的视频地址。\n\n"
                "· 如页面需要点击播放才加载视频, 请点击播放后再次解析\n"
                "· 若捕获到地址但解析时被重定向到占位流(taunt/promo),\n"
                "  说明 token 已过期或缺少 Cookie, 同样请在页签里重新播放后\n"
                "  再解析一次以获取新地址")
            return

        page_url = self._web_sniff_url
        defaults = self._page_headers(page_url)
        headers_map = {it["url"]: self._build_headers(it, defaults)
                       for it in items}
        cookie_n = sum(1 for v in headers_map.values() if v.get("Cookie"))
        blocked = getattr(self._url_interceptor, "blocked", 0)
        self._log_ui(
            f"监听结束, 捕获 {len(urls)} 个候选地址, 开始解析…"
            + (f"(其中 {cookie_n} 个附带 Cookie)" if cookie_n else "")
            + (f" · 已拦截 {blocked} 个广告/统计请求" if blocked else ""))
        # 后台线程解析候选地址(复用静态嗅探的 m3u8 解析逻辑)
        self.sniffer_worker = SnifferWorker(
            page_url,
            use_proxy=not self.ck_noproxy.isChecked(),
            candidate_urls=urls,
            page_title=self._web_sniff_title,
            headers_map=headers_map,
        )
        self.sniffer_worker.finished.connect(self._on_sniff_done)
        self.sniffer_worker.failed.connect(self._on_sniff_fail)
        self.sniffer_worker.log.connect(self._log_ui)
        self.btn_sniff.setEnabled(False)
        self.btn_stop_sniff.setEnabled(True)
        self.sniffer_worker.start()

    # ================================================================ 选择/预览
    def _on_tree_select(self):
        items = self.tree.selectedItems()
        if not items:
            return
        info = items[0].data(0, Qt.UserRole)
        if not isinstance(info, VideoInfo):
            return
        self.current_info = info
        self.ed_title.setText(info.title or "")
        self.ed_save.setText(sanitize_name(info.title or "video"))
        self.lbl_info.setText(
            f"标题: {info.title or '—'}    MIME: {info.mime or '—'}    "
            f"清晰度: {info.resolution or '—'}    码率: {info.bitrate_kbps or '—'} kbps    "
            f"时长: {info.duration_text or '—'}    估算大小: {info.size_text or '—'}"
        )
        self.tabs.setToolTip(self.lbl_info.text())   # 左列紧凑: 详情挂到页签提示
        self._load_preview(info)

    def _memory_preview_url(self, info: VideoInfo) -> Optional[str]:
        """把深度搜索捕获的「内存清单」变成一个可播放的本地地址。

        这类清单原本只存在于页面内存里（站点从不把它发到网络），没有可播放的
        URL。这里把分片/KEY/init 地址全部绝对化后交给预览服务，由它挂到
        ``/mem/<token>.m3u8`` 上再把分片改写成 /p/ 代理地址。

        为什么不能写成本地文件直接播：QtMultimedia 的 FFmpeg 后端在输入是本地
        文件时，HLS 解复用器的协议白名单只有 ``file,crypto,data``，拉 http 分片
        会被拒绝（实测 ``Protocol 'http' not on whitelist``）。
        """
        text = getattr(info, "playlist_text", "") or ""
        if not text:
            return None
        base = info.source or info.url
        try:
            pl = parse_m3u8(text, base)
        except Exception:
            return None
        if not isinstance(pl, MediaPlaylist) or not pl.segments:
            return None
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 f"#EXT-X-TARGETDURATION:{max(1, int(pl.target_duration or 10))}"]
        if pl.init_url:
            lines.append(f'#EXT-X-MAP:URI="{pl.init_url}"')
        if pl.key and pl.key.method != "NONE" and pl.key.uri:
            kv = f'#EXT-X-KEY:METHOD={pl.key.method},URI="{pl.key.uri}"'
            if pl.key.iv:
                kv += f",IV={pl.key.iv}"
            lines.append(kv)
        for s in pl.segments:
            lines.append(f"#EXTINF:{s.duration:.3f},")
            lines.append(s.url)
        lines.append("#EXT-X-ENDLIST")
        try:
            url = self.preview.memory_url("\n".join(lines) + "\n",
                                          info.headers or {}, base)
        except Exception as e:
            self._log_ui(f"内存清单挂载失败: {e}")
            return None
        self._log_ui("预览: 内存清单已挂到本地代理(分片地址已绝对化)")
        return url

    def _load_preview(self, info: VideoInfo):
        """用 QMediaPlayer(FFmpeg) 经本地代理播放。

        不再使用 QtWebEngine + hls.js: 内置 Chromium 是开源编译版, 缺
        H.264/AAC 解码器, 直播流一律播不了(页面报 Error 102630)。
        QMediaPlayer 的 FFmpeg 后端自带完整解码能力。
        """
        if not MEDIA_OK or self.player is None:
            self._log_ui("预览不可用: 缺少 QtMultimedia 组件")
            return
        try:
            local = self._memory_preview_url(info)
            url = local if local else self.preview.proxy_url(
                info.url, info.headers or {})
            self.player.setSource(QUrl(url))
            self.player.play()
            self.tabs.setCurrentIndex(0)
            if self.pv_stack is not None:
                if self.lbl_pv_hint is not None:
                    self.lbl_pv_hint.setText("正在加载预览…")
                self.pv_stack.setCurrentIndex(0)
            self.btn_play.setText("⏸ 暂停")
            self._log_ui(f"预览加载: {info.display_title}")
        except Exception as e:
            self._log_ui(f"预览加载失败: {e}")

    # ---------------------------------------------------------------- 播放器控制
    def _toggle_play(self):
        if self.player is None:
            return
        from PySide6.QtMultimedia import QMediaPlayer as _MP
        if self.player.playbackState() == _MP.PlaybackState.PlayingState:
            self.player.pause()
            self.btn_play.setText("▶ 播放")
        else:
            self.player.play()
            self.btn_play.setText("⏸ 暂停")

    def _stop_preview(self):
        if self.player is None:
            return
        self.player.stop()
        self.btn_play.setText("▶ 播放")
        self._show_pv_placeholder()

    def _on_mute(self, on: bool):
        if self.audio is not None:
            self.audio.setMuted(bool(on))

    def _on_volume(self, val: int):
        if self.audio is not None:
            self.audio.setVolume(max(0, min(100, int(val))) / 100.0)

    def _on_player_error(self, err, msg: str):
        if not msg:
            return
        self._log_ui(f"预览播放失败: {msg}")
        self.btn_play.setText("▶ 播放")
        self._show_pv_placeholder()
        if self.lbl_pv_hint is not None:
            self.lbl_pv_hint.setText(f"预览失败\n{msg}")

    def _on_tree_double_click(self, item, col):
        self._on_download()

    # ================================================================ 下载
    def _parse_custom_headers(self) -> dict:
        text = self.ed_headers.text().strip()
        headers = {}
        if not text:
            return headers
        for part in text.split("|"):
            if ":" in part:
                k, v = part.split(":", 1)
                k, v = k.strip(), v.strip()
                if k:
                    headers[k] = v
        return headers

    def _on_download(self):
        info = self.current_info
        if info is None:
            QMessageBox.warning(self, "提示", "请先在左侧列表中选择要下载的视频")
            return
        workdir = self.ed_workdir.text().strip()
        if not workdir:
            QMessageBox.warning(self, "提示", "请设置工作目录")
            return
        save_name = self.ed_save.text().strip() or "video"
        headers = dict(info.headers or {})
        headers.update(self._parse_custom_headers())
        cfg = DownloadConfig(
            url=info.url,
            save_dir=workdir,
            save_name=save_name,
            headers=headers,
            threads=self.sp_threads.value(),
            retry=self.sp_retry.value(),
            timeout=self.sp_timeout.value(),
            speed_limit=self.sp_speed.value(),
            range_start=self.ed_rstart.text().strip() if self.ck_range.isChecked() else "",
            range_end=self.ed_rend.text().strip() if self.ck_range.isChecked() else "",
            delete_after_merge=self.ck_del.isChecked(),
            binary_merge=self.ck_binmerge.isChecked(),
            no_merge=self.ck_nomerge.isChecked(),
            use_proxy=not self.ck_noproxy.isChecked(),
            # ---- 更多功能(逆向自猫抓) ----
            skip_decrypt=bool(self.adv.get("skip_decrypt")),
            strip_image=bool(self.adv.get("strip_image")),
            extra_query=str(self.adv.get("extra_query") or ""),
            selected=self.adv.get("selected"),
            audio_only=bool(self.adv.get("audio_only")),
            # ---- DASH 音轨(B站): 非空则音视频分别下载后 ffmpeg 合并 ----
            audio_url=getattr(info, "audio_url", "") or "",
            # ---- 深度搜索捕获的内存清单 ----
            playlist_text=getattr(info, "playlist_text", "") or "",
            # 来源页: 请求头回退策略里"宿主页 Referer"那一档要它
            page_url=(getattr(info, "source", "") or
                      self._web_sniff_url or self.web.url().toString() or ""),
        )
        tid = self._next_task_id
        self._next_task_id += 1
        self._tasks[tid] = {"info": info, "cfg": cfg, "state": "排队中", "progress": {}}
        self._task_order.append(tid)
        self._refresh_task_table()
        self._log_ui(f"任务已加入队列: {info.display_title}")
        self._pump_queue()

    def _pump_queue(self):
        if self.download_worker and self.download_worker.isRunning():
            return
        pending = [t for t in self._task_order
                   if self._tasks[t]["state"] in ("排队中",)]
        if not pending:
            self._download_ui_idle()
            return
        tid = pending[0]
        t = self._tasks[tid]
        t["state"] = "下载中"
        self._active_task_id = tid
        self._refresh_task_table()
        worker = DownloadWorker(t["cfg"], ffmpeg_path=self.ffmpeg_path)
        worker.progress.connect(self._on_dl_progress)
        worker.log.connect(self._log_ui)
        worker.state.connect(self._on_dl_state)
        worker.finished.connect(self._on_dl_finished)
        self.download_worker = worker
        self.btn_download.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("暂停")
        self.btn_stop.setEnabled(True)
        worker.start()

    def _download_ui_idle(self):
        self.btn_download.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_pause.setText("暂停")
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.lbl_prog.setText("就绪")

    def _on_dl_progress(self, data: dict):
        if data.get("state") == "done":
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000)
            self.lbl_prog.setText("下载完成")
            # 记录输出文件路径(右键"打开视频"用)
            tid = self._active_task_id
            if tid is not None and tid in self._tasks and data.get("path"):
                self._tasks[tid]["out_path"] = data["path"]
            return
        if data.get("live"):
            self.progress.setRange(0, 0)
            self.lbl_prog.setText(
                f"直播录制中 · 已接收 {format_size(data.get('bytes_done', 0))} · "
                f"已用时 {format_duration(data.get('elapsed', 0))}"
            )
        else:
            pct = min(100.0, data.get("percent", 0) or 0)
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(pct * 10))
            # 只报"整文件"进度: 百分比 + 已下载/总大小 + 速度 + 剩余, 不报分片数
            done_b = data.get("bytes_done", 0) or 0
            total_b = data.get("bytes_total", 0) or 0
            size = (f"{format_size(done_b)} / {format_size(total_b)}"
                    if total_b > 0 else format_size(done_b))
            speed = data.get("speed", 0) or 0
            parts = [f"{pct:.1f}%", size]
            if speed > 0:
                parts.append(f"{format_size(speed)}/s")
                parts.append(f"剩余 {format_duration(data.get('eta', 0) or 0)}")
            self.lbl_prog.setText(" · ".join(parts))
        tid = self._active_task_id
        if tid is not None and tid in self._tasks:
            self._tasks[tid]["progress"] = data
            self._update_task_row(tid)

    def _on_dl_state(self, state: str):
        if state == "paused":
            self.btn_pause.setText("继续")
        elif state == "running":
            self.btn_pause.setText("暂停")

    def _on_dl_finished(self, ok: bool, status: str):
        tid = self._active_task_id
        if tid is not None and tid in self._tasks:
            state_map = {"done": "已完成", "stopped": "已停止", "failed": "失败"}
            self._tasks[tid]["state"] = state_map.get(status, "失败" if not ok else "已完成")
            self._update_task_row(tid)
        self._active_task_id = None
        self.download_worker = None
        self._pump_queue()

    def _on_pause(self):
        if not self.download_worker or not self.download_worker.downloader:
            return
        dl = self.download_worker.downloader
        if dl.paused:
            dl.resume()
            self.btn_pause.setText("暂停")
            self._log_ui("已继续下载")
        else:
            dl.pause()
            self.btn_pause.setText("继续")
            self._log_ui("已暂停下载")

    def _on_stop_download(self):
        if self.download_worker and self.download_worker.downloader:
            self.download_worker.downloader.stop()
            self._log_ui("正在停止当前任务…")

    # ================================================================ 任务表
    def _refresh_task_table(self):
        self.task_table.setRowCount(len(self._task_order))
        for r, tid in enumerate(self._task_order):
            self._fill_task_row(r, tid)

    def _fill_task_row(self, r: int, tid: int):
        t = self._tasks[tid]
        info = t["info"]
        prog = t.get("progress", {})
        if prog.get("live"):
            ptxt = f"{prog.get('done', 0)} 分片"
        elif prog.get("total"):
            ptxt = f"{prog.get('percent', 0):.1f}%"
        else:
            ptxt = "—"
        speed = prog.get("speed", 0)
        stxt = f"{format_size(speed)}/s" if speed else "—"
        vals = [info.display_title, info.resolution or "—", ptxt, stxt, t["state"]]
        for c, v in enumerate(vals):
            item = self.task_table.item(r, c)
            if item is None:
                item = QTableWidgetItem(v)
                self.task_table.setItem(r, c, item)
            else:
                item.setText(v)
        self.task_table.item(r, 0).setData(Qt.UserRole, tid)

    def _update_task_row(self, tid: int):
        if tid not in self._task_order:
            return
        r = self._task_order.index(tid)
        self._fill_task_row(r, tid)

    # ---------------------------------------------------------------- 任务表交互
    def _resolve_out_path(self, tid: int) -> Optional[Path]:
        """定位任务的输出文件: 优先下载完成时记录的路径, 回退按保存名推断。"""
        t = self._tasks.get(tid)
        if not t:
            return None
        p = t.get("out_path") or ""
        if p and Path(p).exists():
            return Path(p)
        cfg = t["cfg"]
        for ext in ("mp4", "ts", "flv"):
            cand = Path(cfg.save_dir) / f"{cfg.save_name}.{ext}"
            if cand.exists():
                return cand
        return None

    def _on_task_menu(self, pos):
        """任务表右键菜单: 打开目录/视频、删除任务、删除文件、清空列表。"""
        menu = QMenu(self)
        item = self.task_table.itemAt(pos)
        tid = None
        if item is not None:
            cell = self.task_table.item(item.row(), 0)
            tid = cell.data(Qt.UserRole) if cell else None

        if tid is not None and tid in self._tasks:
            t = self._tasks[tid]
            downloading = t["state"] == "下载中"
            out_file = self._resolve_out_path(tid)

            act_dir = QAction("打开所在目录", self)
            act_dir.triggered.connect(lambda: self._open_task_dir(tid))
            menu.addAction(act_dir)

            act_video = QAction("打开视频文件", self)
            act_video.setEnabled(out_file is not None)
            act_video.triggered.connect(lambda: self._open_task_video(tid))
            menu.addAction(act_video)
            menu.addSeparator()

            act_del_task = QAction("删除任务", self)
            act_del_task.setEnabled(not downloading)
            act_del_task.setToolTip(
                "仅移除任务记录" if not downloading else "正在下载, 请先停止任务")
            act_del_task.triggered.connect(lambda: self._delete_task(tid))
            menu.addAction(act_del_task)

            act_del_file = QAction("删除文件…", self)
            act_del_file.setEnabled(not downloading)
            act_del_file.triggered.connect(lambda: self._delete_task_file(tid))
            menu.addAction(act_del_file)
            menu.addSeparator()

        act_clear = QAction("清空列表", self)
        act_clear.triggered.connect(self._clear_task_list)
        menu.addAction(act_clear)

        if menu.actions():
            menu.exec(self.task_table.viewport().mapToGlobal(pos))

    def _delete_task(self, tid: int):
        """删除任务记录(不删除已下载的文件)。"""
        t = self._tasks.get(tid)
        if not t:
            return
        if t["state"] == "下载中":
            QMessageBox.information(self, "提示", "该任务正在下载, 请先停止后再删除。")
            return
        name = t["info"].display_title
        del self._tasks[tid]
        if tid in self._task_order:
            self._task_order.remove(tid)
        self._refresh_task_table()
        self._log_ui(f"已删除任务: {name}")
        self._pump_queue()

    def _delete_task_file(self, tid: int):
        """删除任务的视频文件(先弹窗确认), 删除成功后一并移除任务记录。"""
        t = self._tasks.get(tid)
        if not t:
            return
        if t["state"] == "下载中":
            QMessageBox.information(self, "提示", "该任务正在下载, 请先停止后再操作。")
            return
        target = self._resolve_out_path(tid)
        if target is None:
            QMessageBox.information(
                self, "提示",
                "未找到该任务的视频文件(可能尚未完成或被移动)。")
            return
        ret = QMessageBox.question(
            self, "删除视频文件",
            f"是否删除该视频文件?\n\n{target}\n\n此操作不可恢复。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        try:
            size = target.stat().st_size
            target.unlink()
        except Exception as e:
            QMessageBox.critical(self, "删除失败", f"无法删除文件:\n{e}")
            return
        self._log_ui(f"已删除文件: {target} ({format_size(size)})")
        # 文件已删, 同步移除任务记录
        if tid in self._tasks:
            del self._tasks[tid]
        if tid in self._task_order:
            self._task_order.remove(tid)
        self._refresh_task_table()
        self._pump_queue()

    def _open_task_dir(self, tid: int):
        t = self._tasks.get(tid)
        if not t:
            return
        d = Path(t["cfg"].save_dir)
        if d.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))
        else:
            self._log_ui(f"目录不存在: {d}")

    def _open_task_video(self, tid: int):
        p = self._resolve_out_path(tid)
        if p is None:
            QMessageBox.information(self, "提示", "尚未找到输出文件(任务可能未完成)。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _clear_task_list(self):
        """清空列表: 移除选中行(未选中则清空全部), 跳过正在下载的任务。"""
        rows = sorted({i.row() for i in self.task_table.selectedItems()},
                      reverse=True)
        if not rows:
            if not self._task_order:
                return
            ret = QMessageBox.question(
                self, "清空列表",
                "未选中任何任务, 将清空全部任务记录(不会删除已下载的文件)。\n确定继续吗?",
                QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                return
            rows = list(range(self.task_table.rowCount()))
        removed = skipped = 0
        for r in rows:
            cell = self.task_table.item(r, 0)
            if cell is None:
                continue
            tid = cell.data(Qt.UserRole)
            t = self._tasks.get(tid)
            if t is None:
                continue
            if t["state"] == "下载中":
                skipped += 1
                continue
            del self._tasks[tid]
            if tid in self._task_order:
                self._task_order.remove(tid)
            removed += 1
        self._refresh_task_table()
        if skipped:
            self._log_ui(f"已清空 {removed} 条任务记录(跳过 {skipped} 个正在下载的任务)")
        else:
            self._log_ui(f"已清空 {removed} 条任务记录")
        self._pump_queue()

    def _sync_task_order(self):
        """拖动重排序后: 按表格当前行序重建任务队列顺序。"""
        order = []
        for r in range(self.task_table.rowCount()):
            cell = self.task_table.item(r, 0)
            if cell is not None:
                tid = cell.data(Qt.UserRole)
                if tid is not None and tid not in order:
                    order.append(tid)
        self._task_order = [t for t in order if t in self._tasks]
        self._log_ui("任务顺序已调整")

    # ================================================================ 其他
    def _choose_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择工作目录", self.ed_workdir.text())
        if d:
            self.ed_workdir.setText(d)

    def _check_ffmpeg_manual(self):
        p = find_ffmpeg()
        self._refresh_ffmpeg_status()
        if p:
            ver = ffmpeg_version(p)
            QMessageBox.information(self, "ffmpeg 环境", f"已找到 ffmpeg:\n{p}\n\n{ver}")
        else:
            ret = QMessageBox.question(
                self, "ffmpeg 环境",
                "未检测到 ffmpeg。\n\n"
                "· TS 分片: 可使用二进制合并直接输出, 无需 ffmpeg\n"
                "· fMP4/MP4 分片或需要输出 MP4: 需要 ffmpeg\n\n"
                "是否打开官方下载页面?",
                QMessageBox.Yes | QMessageBox.No)
            if ret == QMessageBox.Yes:
                QDesktopServices.openUrl(QUrl(FFMPEG_DOWNLOAD_PAGE))

    def _clear_web_cache(self):
        """清理内置浏览器累积的网站缓存(HTTP 缓存 + 访问记录, 保留 Cookie)。"""
        if not WEBENGINE_OK:
            QMessageBox.information(self, "提示", "当前环境不支持浏览器引擎。")
            return
        ret = QMessageBox.question(
            self, "清理网站缓存",
            "将清除内置浏览器累积的网页缓存与访问记录。\n"
            "(不包含登录 Cookie, 避免网站登录状态丢失)\n\n确定继续吗?",
            QMessageBox.Yes | QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        profile = QWebEngineProfile.defaultProfile()
        profile.clearHttpCache()
        profile.clearAllVisitedLinks()
        cp = profile.cachePath()
        self._log_ui("已清理网站缓存(HTTP 缓存 + 访问记录)"
                     + (f": {cp}" if cp else ""))
        QMessageBox.information(self, "清理网站缓存", "网站缓存已清理完成。")

    def _show_about(self, *_args):
        dlg = AboutDialog(self)
        dlg.exec()

    def _log_ui(self, msg: str):
        self.log_view.appendPlainText(msg)

    # ================================================================ 关闭
    def _user_settings(self):
        """记住用户拖动过的分栏宽度(下次启动沿用)。"""
        from PySide6.QtCore import QSettings

        return QSettings("SiYueAperture", "M3U8VideoDownloader")

    def _split_sizes(self):
        """左右列初始宽度: 优先用上次拖动的结果, 否则用默认比例。"""
        try:
            saved = self._user_settings().value("ui/split_sizes")
            if saved:
                vals = [int(x) for x in str(saved).split(",") if x.strip()]
                if len(vals) == 2 and all(v >= 200 for v in vals):
                    return vals
        except Exception:
            pass
        return list(DEFAULT_SPLIT_SIZES)

    def _save_split_sizes(self):
        try:
            w = self.main_split.sizes()
            if len(w) == 2 and all(v >= 200 for v in w):
                self._user_settings().setValue(
                    "ui/split_sizes", f"{int(w[0])},{int(w[1])}")
        except Exception:
            pass

    # ------------------------------------------------- 五点簇位置(可拖)
    def _grip_pos(self, key: str) -> float:
        """读取五点簇上次停在拖动条的哪个比例位置(0~1)。"""
        try:
            saved = self._user_settings().value(key)
            if saved is not None:
                v = float(saved)
                if 0.0 <= v <= 1.0:
                    return v
        except Exception:
            pass
        return DEFAULT_GRIP_POS

    def _save_grip_pos(self, key: str, splitter):
        """五点簇的位置**只在退出时落盘**(与 split 尺寸同一套理由: 拖动过程中
        窗口缩放/重新布局会反复触发, 存中间值没意义)。"""
        try:
            if splitter is not None:
                self._user_settings().setValue(key, f"{splitter.grip_pos():.3f}")
        except Exception:
            pass

    def closeEvent(self, event):
        _dbg(f"closeEvent spontaneous={event.spontaneous()}")
        self._save_split_sizes()
        self._save_bottom_split_sizes()
        self._save_grip_pos("ui/grip_lr", self.main_split)
        self._save_grip_pos("ui/grip_tb", self.bottom_split)
        if self.download_worker and self.download_worker.isRunning():
            ret = QMessageBox.question(
                self, "确认退出", "当前有下载任务正在进行, 确定要退出吗?",
                QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                event.ignore()
                return
            if self.download_worker.downloader:
                self.download_worker.downloader.stop()
            self.download_worker.wait(3000)
        # 系统浏览器是独立进程, 不主动收掉会留下一个「孤儿」浏览器窗口
        self._closing = True
        if self._cdp is not None and self._cdp.active:
            try:
                self._cdp.stop(close_browser=True)
            except Exception:
                pass
        try:
            if self.player is not None:
                self.player.stop()
                self.player.setSource(QUrl())
        except Exception:
            pass
        # 缓存捕捉的临时文件随退出清理(TEMP 目录, 不占用户空间)
        try:
            self._cap_timer.stop()
            self._capture.clear()
        except Exception:
            pass
        self.preview.stop()
        event.accept()
