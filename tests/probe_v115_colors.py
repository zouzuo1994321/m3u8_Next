"""临时探针: v1.1.15 三处颜色/边框改动的逐像素验收。

1. 预览区 #pvBox 不应再有 1px 描边
2. 静音勾选框 -> 深蓝底白勾
3. 音量条 sub-page / handle -> 蓝色
4. 分栏五点 -> 蓝色

用法: python tests/probe_v115_colors.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from m3u8dl.ui.main_window import _DotsHandle, MainWindow  # noqa: E402
from m3u8dl.ui.theme import DARK_QSS  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "shots"
OUT.mkdir(parents=True, exist_ok=True)

app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(DARK_QSS)
win = MainWindow()
win.show()

RESULT = []


def chk(cond, msg):
    RESULT.append(bool(cond))
    print(("OK   " if cond else "FAIL ") + msg)


def shot():
    box = win.findChild(type(win.centralWidget()), "pvBox")
    from PySide6.QtWidgets import QFrame
    box = win.findChild(QFrame, "pvBox")
    # v1.1.20 起五点簇位置可从设置读回, 而且用户/其它探针可能把它拖离中点。
    # 取色前先归位到 0.5, 保证这一张图与取样点稳定(否则簇不在中点上,
    # 取到的就是把手底色, 会误判成"五点不蓝")。
    win.main_split.set_grip_pos(0.5)
    win.bottom_split.set_grip_pos(0.5)
    for _ in range(3):
        app.processEvents()
    img = win.grab().toImage()

    # ---- 1. pvBox 描边 ----
    if box is not None:
        g = box.geometry()
        tl = box.mapTo(win, box.rect().topLeft())
        # 上边框: 取容器顶行中间像素, 应仍是纯黑(而非 #3a4150)
        px = img.pixelColor(tl.x() + g.width() // 2, tl.y())
        chk(px.red() < 12 and px.green() < 12 and px.blue() < 12,
            f"pvBox 顶边无描边 (rgb={px.red()},{px.green()},{px.blue()})")
        px2 = img.pixelColor(tl.x(), tl.y() + g.height() // 2)
        chk(px2.red() < 12 and px2.green() < 12 and px2.blue() < 12,
            f"pvBox 左边无描边 (rgb={px2.red()},{px2.green()},{px2.blue()})")

    # ---- 2. 静音勾选框配色 ----
    chk(win.ck_mute._on.name() == "#1e40af",
        f"静音勾选框主色为深蓝 ({win.ck_mute._on.name()})")
    chk(win.ck_mute.isChecked(), "静音默认勾选(便于取色)")
    ck_pos = win.ck_mute.mapTo(win, win.ck_mute.rect().topLeft())
    # 勾选框 16x16 在控件左侧, 垂直居中; 中心被白色对勾占住, 要整块取样
    base_x = ck_pos.x()
    base_y = ck_pos.y() + (win.ck_mute.height() - 16) // 2
    blues, whites = 0, 0
    for dx in range(2, 14):
        for dy in range(2, 14):
            p = img.pixelColor(base_x + dx, base_y + dy)
            if p.blue() > p.red() + 40 and p.blue() > 120:
                blues += 1
            elif p.red() > 200 and p.green() > 200 and p.blue() > 200:
                whites += 1
    chk(blues >= 40, f"静音勾选框深蓝底面积够 (blue px={blues})")
    chk(whites >= 4, f"静音勾选框仍有白色对勾 (white px={whites})")

    # ---- 3. 音量条 ----
    sl = win.sl_vol
    sl_pos = sl.mapTo(win, sl.rect().topLeft())
    # 滑块在 70% 位置, 取该点
    hx = sl_pos.x() + int(sl.width() * 0.70)
    hy = sl_pos.y() + sl.height() // 2
    hp = img.pixelColor(hx, hy)
    chk(hp.blue() > hp.red(), f"滑块为蓝色系 (rgb={hp.red()},{hp.green()},{hp.blue()})")
    # sub-page: 取 20% 处(已填充段)
    sx = sl_pos.x() + int(sl.width() * 0.20)
    sp = img.pixelColor(sx, hy)
    chk(sp.blue() > sp.red() + 30,
        f"音量条已填充段为蓝 (rgb={sp.red()},{sp.green()},{sp.blue()})")

    # ---- 4. 分栏五点 ----
    chk(_DotsHandle._DOT.name() == "#2f6bd8",
        f"五点色为深蓝 ({_DotsHandle._DOT.name()})")
    chk(_DotsHandle._DOT_HOVER.name() == "#7aa8f5",
        f"五点悬停色为浅蓝 ({_DotsHandle._DOT_HOVER.name()})")

    # ---- 出图 ----
    bar = OUT / "v115_controlbar.png"
    if win.btn_play and win.sl_vol:
        x0 = win.btn_play.mapTo(win, win.btn_play.rect().topLeft()).x() - 4
        y0 = win.btn_play.mapTo(win, win.btn_play.rect().topLeft()).y() - 4
        x1 = win.sl_vol.mapTo(win, win.sl_vol.rect().bottomRight()).x() + 4
        y1 = win.sl_vol.mapTo(win, win.sl_vol.rect().bottomRight()).y() + 4
        crop = img.copy(x0, y0, max(1, x1 - x0), max(1, y1 - y0))
        crop.scaled(crop.width() * 4, crop.height() * 4).save(str(bar))
        print(f"控制条: {x0},{y0} {x1 - x0}x{y1 - y0} -> {bar}")
    if box is not None:
        g = box.geometry()
        tl = box.mapTo(win, box.rect().topLeft())
        crop = img.copy(tl.x(), tl.y(), g.width(), g.height())
        pv = OUT / "v115_pvbox.png"
        crop.scaled(crop.width() * 2, crop.height() * 2).save(str(pv))
        print(f"预览框 -> {pv}")
    sp2 = OUT / "v115_split.png"
    h = win.main_split.handle(1)
    if h is not None:
        hp = h.mapTo(win, h.rect().topLeft())
        # 簇中心由 grip_pos 决定, 不再假设"就在把手几何中点"
        gy = int(h._grip_center())
        crop = img.copy(hp.x() - 6, hp.y() + gy - 24, h.width() + 12, 48)
        crop.scaled(crop.width() * 5, crop.height() * 5).save(str(sp2))
        print(f"分栏条: 把手 y={hp.y()} 簇中心 y={hp.y() + gy} -> {sp2}")
        dp = img.pixelColor(hp.x() + h.width() // 2, hp.y() + gy)
        chk(dp.blue() > dp.red() + 40,
            f"五点实际像素偏蓝 (rgb={dp.red()},{dp.green()},{dp.blue()})")

    print(f"\n{sum(RESULT)}/{len(RESULT)} passed")
    print("RESULT=" + ("OK" if all(RESULT) else "FAIL"))
    app.quit()


QTimer.singleShot(2200, shot)
app.exec()
