"""启动动效预览: 把 SplashWindow 在几个时间点渲染成静态图, 便于核对视觉。

用法: python tests/probe_splash.py
输出: docs/shots/splash_t*.png (拼成一张 splash_strip.png)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from m3u8dl import version  # noqa: E402
from m3u8dl.ui import splash as S  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "shots"
OUT.mkdir(parents=True, exist_ok=True)

app = QApplication(sys.argv)
app.setStyle("Fusion")

assets = Path(__file__).resolve().parent.parent / "assets"
w = S.build_splash(assets, version.APP_NAME,
                   f"{version.VERSION} · Build {version.BUILD}")
if w is None:
    print("RESULT=FAIL 没有 icon.png/ico")
    sys.exit(1)

shots = []
for frac in (0.05, 0.16, 0.30, 0.55, 0.86, 0.95):
    w._t = frac
    w.update()
    img = QPixmap(w.width() + 40, w.height() + 40)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.fillRect(img.rect(), Qt.black)      # 模拟桌面暗底, 便于看透明度
    p.end()
    w.render(img, QPoint(20, 20))
    f = OUT / f"splash_t{int(frac * 100):02d}.png"
    img.save(str(f))
    shots.append(f)
    print("saved", f.name, f"t={frac}")

# 拼成横向长条
from PIL import Image  # noqa: E402

ims = [Image.open(p) for p in shots]
W = sum(i.width for i in ims) + 8 * (len(ims) - 1)
H = max(i.height for i in ims)
strip = Image.new("RGB", (W, H), (8, 8, 10))
x = 0
for i in ims:
    strip.paste(i.convert("RGB"), (x, 0))
    x += i.width + 8
strip.save(str(OUT / "splash_strip.png"))
print("strip ->", OUT / "splash_strip.png")
print("RESULT=OK")
