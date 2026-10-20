"""临时探针: v1.1.20 五点簇可拖(两种手势互不干扰)。

验证:
1. 抓在五点上拖 -> 只挪高亮位置, 左右列宽 / 上下行高不变
2. 抓在五点以外 -> 不进"抓点"分支(原生的调宽高仍由 QSplitterHandle 处理)
3. 位置能落盘/读回
4. 三个位置的实拍截图

说明: QSplitter 真正拖动改宽高靠的是**真实鼠标抓取**(内部 handleMousePress
才 grabMouse), 合成事件驱动不动, 所以第 2 项只断言"没有被我们劫持"。

用法: python tests/probe_grip.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from m3u8dl.ui.main_window import MainWindow  # noqa: E402
from m3u8dl.ui.theme import DARK_QSS  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "shots"
OUT.mkdir(parents=True, exist_ok=True)

app = QApplication(sys.argv)
app.setStyle("Fusion")
app.setStyleSheet(DARK_QSS)

RESULT = []


def chk(cond, msg):
    RESULT.append(bool(cond))
    print(("OK   " if cond else "FAIL ") + msg)


win = MainWindow()
win.show()
for _ in range(12):
    app.processEvents()

win.main_split.set_grip_pos(0.5)
win.bottom_split.set_grip_pos(0.5)
app.processEvents()


def ev(handle, kind, x, y):
    """合成鼠标事件(globalPos 按把手实际位置换算, 与真实事件一致)。"""
    gp = handle.mapToGlobal(QPointF(x, y).toPoint())
    return QMouseEvent(kind, QPointF(x, y), QPointF(gp),
                       Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)


def click_drag(handle, horiz, start_v, end_v):
    """在把手上按 start_v 按下、拖到 end_v 松开。"""
    other = (handle.width() / 2.0) if horiz else (handle.height() / 2.0)

    def at(v):
        return (other, v) if horiz else (v, other)

    s, e = at(start_v), at(end_v)
    handle.mousePressEvent(ev(handle, QEvent.MouseButtonPress, *s))
    handle.mouseMoveEvent(ev(handle, QEvent.MouseMove, *e))
    handle.mouseReleaseEvent(ev(handle, QEvent.MouseButtonRelease, *e))
    app.processEvents()


# ---------------------------------------------------------------- 1. 横向抓点
h = win.main_split.handle(1)
before = list(win.main_split.sizes())
c = h._grip_center()
click_drag(h, True, c, c + 70)
pos1 = h.grip_pos()
print(f"handle h={h.height()} cluster 0.5@{c:.0f} -> {pos1:.3f}@{h._grip_center():.0f}")
print(f"main_split sizes {before} -> {list(win.main_split.sizes())}")
chk(abs(pos1 - 0.5) > 0.05, f"抓点拖动后位置应变化 (0.5 -> {pos1:.3f})")
chk(list(win.main_split.sizes()) == before, "抓点拖动不应改变左右列宽")
chk(abs(h._grip_center() - (c + 70)) <= 8,
    f"五点簇应跟到鼠标处 (期望 ~{c + 70:.0f}, 实际 {h._grip_center():.0f})")
chk(not h._grip_drag, "松开后应退出抓点状态")

# ---------------------------------------------------------------- 2. 横向抓虚线
pos_before = h.grip_pos()
click_drag(h, True, 20, 90)          # 远离簇(簇已在 c+70)
print(f"grip {pos_before:.3f} -> {h.grip_pos():.3f}")
chk(abs(h.grip_pos() - pos_before) < 1e-6, "抓五点以外不应挪动五点(交给原生拖动)")
chk(not h._grip_drag, "五点以外按下不应进入抓点状态")

# ---------------------------------------------------------------- 3. 竖向
hb = win.bottom_split.handle(1)
beforeb = list(win.bottom_split.sizes())
cb = hb._grip_center()
click_drag(hb, False, cb, cb - 40)
print(f"bottom sizes {beforeb} -> {list(win.bottom_split.sizes())}, pos={hb.grip_pos():.3f}")
chk(list(win.bottom_split.sizes()) == beforeb, "竖向抓点拖动不应改变行高")
chk(abs(hb.grip_pos() - 0.5) > 0.03, f"竖向位置应变化 (实际 {hb.grip_pos():.3f})")

# ---------------------------------------------------------------- 4. 落盘
win._save_grip_pos("ui/grip_lr", win.main_split)
win._save_grip_pos("ui/grip_tb", win.bottom_split)
chk(abs(win._grip_pos("ui/grip_lr") - win.main_split.grip_pos()) < 1e-3,
    "主分栏位置可落盘/读回")
chk(abs(win._grip_pos("ui/grip_tb") - win.bottom_split.grip_pos()) < 1e-3,
    "竖向位置可落盘/读回")

# ---------------------------------------------------------------- 5. 截图
shots = []
for frac, name in ((0.12, "grip_top"), (0.5, "grip_center"), (0.88, "grip_bottom")):
    win.main_split.set_grip_pos(frac)
    win.bottom_split.set_grip_pos(frac)
    for _ in range(3):
        app.processEvents()
    # 直接抓把手本身, 3 倍放大: 一条竖线 + 五点簇停在对应位置
    shot = h.grab().scaled(h.width() * 3, h.height() * 3,
                           Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    shot.save(str(OUT / f"{name}.png"))
    shots.append(shot)
    print(f"saved {name}.png  pos={frac}")

# 三张并排拼成一张, 一眼看出"高亮位置可调"
gap = 12
W = sum(s.width() for s in shots) + gap * (len(shots) - 1)
H = max(s.height() for s in shots)
mosaic = QImage(W, H, QImage.Format_ARGB32)
mosaic.fill(QColor(0x14, 0x16, 0x1B))
mp = QPainter(mosaic)
try:
    x = 0
    for s in shots:
        mp.drawImage(x, 0, s.toImage())
        x += s.width() + gap
finally:
    mp.end()
mosaic.save(str(OUT / "grip_positions.png"))
print(f"saved grip_positions.png {W}x{H}")

# 整窗实拍: 两个把手的高亮都停在偏上的位置
win.main_split.set_grip_pos(0.25)
win.bottom_split.set_grip_pos(0.25)
for _ in range(4):
    app.processEvents()
win.grab().save(str(OUT / "grip_live.png"))
print("saved grip_live.png")

# 归位到默认 0.5 再收尾 —— 注意顺序: closeEvent 会把当前 grip 位置**落盘**,
# 所以必须"先归位 -> 再 close -> 最后才 remove", 否则 close 又把 0.25 写回去
# (v1.1.21 踩过: 探针跑完后五点簇永远停在 1/4 处, 连带 probe_v115_colors 取色失败)
win.main_split.set_grip_pos(0.5)
win.bottom_split.set_grip_pos(0.5)
for _ in range(3):
    app.processEvents()

print(f"\n{sum(RESULT)}/{len(RESULT)} passed")
win.close()
app.processEvents()
for key in ("ui/grip_lr", "ui/grip_tb"):
    win._user_settings().remove(key)
win._user_settings().sync()
sys.stdout.flush()
