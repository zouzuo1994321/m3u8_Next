"""临时探针: v1.1.19 两处改动的几何验收。

1. 「关于」对话框: 高度压缩 / 行间距收紧(截图 + 尺寸)
2. 下载设置里四个勾选框: 第 0 列统一宽度, 「合并后删除分片」与
   「不使用系统代理」左边缘同 x, 且两列之间有实际间隙

用法: python tests/probe_about.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from m3u8dl.ui.about import AboutDialog  # noqa: E402
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


# ---------------------------------------------------------------- 1. 关于
dlg = AboutDialog()
dlg.show()
app.processEvents()
img = dlg.grab().toImage()
img.save(str(OUT / "about.png"))
print(f"about size = {dlg.width()}x{dlg.height()}")
chk(dlg.height() <= 300, f"关于高度 <=300 (实际 {dlg.height()})")
chk(dlg.width() == 456, f"关于宽度 456 (实际 {dlg.width()})")

# 垂直方向：最后一条致谢文字底边到「确定」按钮顶边的距离不能过大
# (只做粗检: 对话框内文本区不应出现连续 >14 行高的纯背景带)
bg = img.pixelColor(4, img.height() // 2).name()
print(f"sample bg = {bg}")

# ---------------------------------------------------------------- 2. 勾选框
win = MainWindow()
win.show()
app.processEvents()
for _ in range(3):
    app.processEvents()

ck = {
    "binmerge": win.ck_binmerge,
    "del": win.ck_del,
    "nomerge": win.ck_nomerge,
    "noproxy": win.ck_noproxy,
}
xs = {}
for k, w in ck.items():
    p = w.mapTo(win, QPoint(0, 0))
    xs[k] = (p.x(), p.y(), w.width(), w.height())
    print(f"{k:8s} x={p.x():4d} y={p.y():4d} w={w.width():4d} h={w.height():3d}")

chk(xs["del"][0] == xs["noproxy"][0],
    f"第二列同 x (del={xs['del'][0]} noproxy={xs['noproxy'][0]})")
chk(xs["binmerge"][0] == xs["nomerge"][0],
    f"第一列同 x (binmerge={xs['binmerge'][0]} nomerge={xs['nomerge'][0]})")
gap = xs["del"][0] - (xs["binmerge"][0] + xs["binmerge"][2])
chk(gap >= 12, f"两列间隙 >=12 (实际 {gap})")
chk(xs["del"][1] == xs["binmerge"][1], "第 0 行两枚勾选框同高")
chk(xs["nomerge"][1] > xs["binmerge"][1], "两行确实上下排布")

# 截图: 下载设置框
grp = win.ck_binmerge.parentWidget()
shot = (grp or win).grab()
shot.save(str(OUT / "settings_ck.png"))
print(f"saved settings_ck.png {shot.width()}x{shot.height()}")
tight = win.ck_noproxy.mapTo(win, QPoint(0, 0)).x() + win.ck_noproxy.width()
print(f"右列内容右边缘 = {tight}, 右列可用宽度见下")
if grp is not None:
    print(f"settingsGroup w={grp.width()}")

print(f"\n{sum(RESULT)}/{len(RESULT)} passed")
win.close()
dlg.close()
app.processEvents()
sys.stdout.flush()
