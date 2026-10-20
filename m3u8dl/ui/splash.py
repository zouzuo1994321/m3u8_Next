"""启动动效: 图标「长大」成主窗口。

为什么不用 QSplashScreen
------------------------
``QSplashScreen`` 只会贴一张静态图, 没法做"图标放大 + 卡面淡出"的过渡, 而且
它的背景是不透明的 —— 圆角卡片周围会露出一个方块。这里自己画: 窗口设为
``WA_TranslucentBackground`` + 无边框, 圆角/阴影/进度条全部用 QPainter 绘制。

动画分三段(总时长 ``TOTAL_MS``):
    0.00 → 0.28  图标从 0.72 倍放大到 1 倍并淡入, 卡面同时浮现
    0.15 → 0.45  标题与版本文案淡入
    0.30 → 0.85  底部进度条来回扫, 文案切换成"正在加载…"
    0.85 → 1.00  整张卡放大到 1.15 倍并淡出 —— 视觉上"化进"主窗口

主窗口在最后一帧出现(windowOpacity 0→1), 两段动画首尾相接, 不会闪白。
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QEasingCurve, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget

#: 整段动效时长(毫秒)。1.5s 是"看得见但不拖沓"的经验值:
#: 比 1s 短就来不及看清图标, 超过 2.5s 用户会开始烦躁。
TOTAL_MS = 1500

_CARD_W, _CARD_H = 340, 396
_BG = QColor(0x14, 0x16, 0x1B)
_BD = QColor(0x2C, 0x31, 0x3C)
_TITLE = QColor(0xE6, 0xE3, 0xDD)
_SUB = QColor(0x7A, 0x82, 0x90)
_TRACK = QColor(0x23, 0x27, 0x2F)
_ACCENT = QColor(0x2B, 0x57, 0xCF)
_ACCENT2 = QColor(0x5B, 0x93, 0xF0)


def _ease_out(p: float) -> float:
    """三次缓出: 起步快、收尾稳。"""
    return 1.0 - (1.0 - p) ** 3


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def _seg(t: float, a: float, b: float) -> float:
    """把全局进度 t 映射到 [a,b] 区间的 0~1。"""
    if b <= a:
        return 1.0 if t >= b else 0.0
    return _clamp01((t - a) / (b - a))


class SplashWindow(QWidget):
    """启动卡片。调用 :meth:`play` 开始, 结束后回调。"""

    def __init__(self, logo: Path | QPixmap, title: str, subtitle: str,
                 parent=None):
        super().__init__(parent, Qt.SplashScreen | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(_CARD_W, _CARD_H)
        self.setCursor(Qt.PointingHandCursor)
        if isinstance(logo, QPixmap):
            self._pm = logo
        else:
            self._pm = QPixmap(str(logo))
        self._title = title
        self._subtitle = subtitle
        self._t = 0.0
        self._on_done: Optional[Callable[[], None]] = None
        self._fired = False
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(TOTAL_MS)
        self._anim.setEasingCurve(QEasingCurve.Linear)
        self._anim.valueChanged.connect(self._tick)
        self._anim.finished.connect(self._finish)

    # ------------------------------------------------------------ 对外接口
    def play(self, on_done: Callable[[], None]):
        """居中显示并播放动效, 播完(或被点击跳过)后调用 ``on_done``。"""
        self._on_done = on_done
        self._center()
        self.show()
        self.raise_()
        self._anim.start()

    def skip(self):
        if not self._fired:
            self._finish()

    # ------------------------------------------------------------ 内部
    def _center(self):
        scr = self.screen()
        if scr is None:
            return
        g = scr.availableGeometry()
        self.move(g.center().x() - self.width() // 2,
                  g.center().y() - self.height() // 2)

    def _tick(self, v):
        self._t = float(v)
        self.update()

    def _finish(self):
        if self._fired:
            return
        self._fired = True
        try:
            self._anim.stop()
        except Exception:
            pass
        self.hide()
        cb = self._on_done
        self._on_done = None
        if cb is not None:
            cb()
        # 让调用方有机会把它 deleteLater, 这里先不 close 以免事件循环里报错
        QTimer.singleShot(0, self.close)

    def mousePressEvent(self, event):        # noqa: N802
        self.skip()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):          # noqa: N802
        if event.key() in (Qt.Key_Escape, Qt.Key_Space, Qt.Key_Return):
            self.skip()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event):             # noqa: N802
        t = self._t
        # 整张卡的缩放/透明度: 前段 0.98→1.0 轻微"落定", 末段放大淡出
        card_scale = 1.0
        card_alpha = 1.0
        if t < 0.10:
            k = _ease_out(_seg(t, 0.0, 0.10))
            card_scale = 0.96 + 0.04 * k
            card_alpha = k
        elif t > 0.85:
            k = _seg(t, 0.85, 1.0)
            card_scale = 1.0 + 0.15 * k
            card_alpha = 1.0 - k

        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.setOpacity(card_alpha)
            p.translate(self.width() / 2.0, self.height() / 2.0)
            p.scale(card_scale, card_scale)
            p.translate(-self.width() / 2.0, -self.height() / 2.0)

            # 每段自己算乘法系数后统一设一次 opacity: 不要"读出 p.opacity()
            # 再乘再除", 因为 setOpacity 会把值夹到 [0,1], 除回来就失真了
            self._paint_card(p)
            self._paint_logo(p, t, card_alpha)
            self._paint_text(p, t, card_alpha)
            self._paint_progress(p, t, card_alpha)
        finally:
            p.end()

    def _paint_card(self, p: QPainter):
        r = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        # 外发光: 由外向内叠三层半透明, 暗色桌面上能看出卡片"浮起来"
        for i, a in ((10, 8), (6, 12), (3, 18)):
            glow = QColor(_ACCENT)
            glow.setAlpha(a)
            p.setPen(QPen(glow, i * 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(-i, -i, i, i), 18 + i, 18 + i)
        path = QPainterPath()
        path.addRoundedRect(r, 18, 18)
        p.fillPath(path, _BG)
        p.setPen(QPen(_BD, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 18, 18)

    def _paint_logo(self, p: QPainter, t: float, alpha: float):
        if self._pm.isNull():
            return
        k = _ease_out(_seg(t, 0.0, 0.28))
        side = 132.0 * (0.72 + 0.28 * k)
        cx, cy = self.width() / 2.0, 152.0
        p.setOpacity(alpha * k)
        p.drawPixmap(QRectF(cx - side / 2, cy - side / 2, side, side),
                     self._pm, QRectF(self._pm.rect()))
        p.setOpacity(alpha)

    def _paint_text(self, p: QPainter, t: float, alpha: float):
        k = _ease_out(_seg(t, 0.15, 0.45))
        if k <= 0.0:
            return
        a = alpha
        p.setOpacity(a * k)
        f = QFont(self.font())
        f.setPixelSize(19)
        f.setBold(True)
        p.setFont(f)
        p.setPen(_TITLE)
        p.drawText(QRectF(0, 258, self.width(), 28),
                   Qt.AlignHCenter | Qt.AlignVCenter, self._title)

        f2 = QFont(self.font())
        f2.setPixelSize(11)
        p.setFont(f2)
        p.setPen(_SUB)
        p.drawText(QRectF(0, 288, self.width(), 20),
                   Qt.AlignHCenter | Qt.AlignVCenter, self._subtitle)
        p.setOpacity(a)

    def _paint_progress(self, p: QPainter, t: float, alpha: float):
        k = _seg(t, 0.22, 0.35)
        if k <= 0.0:
            return
        a = alpha * k
        track_w, track_h = 220.0, 3.0
        x = (self.width() - track_w) / 2.0
        y = 340.0
        p.setOpacity(a)
        p.setPen(Qt.NoPen)
        p.setBrush(_TRACK)
        p.drawRoundedRect(QRectF(x, y, track_w, track_h), 1.5, 1.5)

        # 来回扫的进度块: 用正弦让两端有"减速"感, 比线性来回自然
        phase = _seg(t, 0.30, 1.0)
        pos = 0.5 - 0.5 * math.cos(phase * math.pi * 2.0)
        seg_w = 86.0
        sx = x + (track_w - seg_w) * pos
        p.setBrush(_ACCENT2)
        p.drawRoundedRect(QRectF(sx, y, seg_w, track_h), 1.5, 1.5)

        # 状态文案
        f = QFont(self.font())
        f.setPixelSize(11)
        p.setFont(f)
        p.setPen(QColor(0x6E, 0x76, 0x86))
        status = "正在初始化…" if t < 0.55 else "正在加载界面…"
        p.drawText(QRectF(0, y + 12, self.width(), 18),
                   Qt.AlignHCenter | Qt.AlignVCenter, status)
        p.setOpacity(a)


def reveal_window(win: QWidget, duration_ms: int = 280):
    """主窗口淡入(配合卡片淡出, 两边加起来不是硬切)。"""
    from PySide6.QtCore import QPropertyAnimation

    try:
        win.setWindowOpacity(0.0)
        win.show()
        anim = QPropertyAnimation(win, b"windowOpacity", win)
        anim.setDuration(duration_ms)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)

        def _done():
            try:
                win.setWindowOpacity(1.0)     # 兜底: 动画被打断也不会半透明卡住
            except Exception:
                pass

        anim.finished.connect(_done)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
        # 持有引用, 免得被 GC 掉导致动画只跑第一帧
        win._reveal_anim = anim       # type: ignore[attr-defined]
        return anim
    except Exception:
        try:
            win.setWindowOpacity(1.0)
            win.show()
        except Exception:
            pass
        return None


def build_splash(assets, title: str, subtitle: str) -> Optional[SplashWindow]:
    """按素材目录造一个启动卡片; 图片缺失时返回 None(降级为直接显示窗口)。"""
    try:
        for name in ("icon.png", "icon.ico"):
            f = Path(assets) / name
            if f.exists():
                pm = QPixmap(str(f))
                if not pm.isNull():
                    return SplashWindow(pm, title, subtitle)
    except Exception:
        pass
    return None
