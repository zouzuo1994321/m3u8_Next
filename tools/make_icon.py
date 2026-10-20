"""由项目根目录的 logo.png 生成应用图标(assets/icon.ico + assets/icon.png)。

用法:
    python tools/make_icon.py            # 用根目录 logo.png
    python tools/make_icon.py 某图.png   # 指定源图

源图要求: 正方形、圆角/圆形主体、四角透明(本项目的 logo.png 已满足)。
脚本会先按不透明区域裁掉多余留白, 让图标在 16px 时也尽可能占满画布。

为什么小尺寸要单独重采样: 900px 直接缩到 16px 时, 字母 M 的细笔画会被
均值糊掉(灰蒙蒙一片)。16/24/32 这些尺寸改用"逐步减半 + 轻锐化",
边缘才不会发虚。
"""
import sys
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"
#: .ico 里要放的尺寸(Windows 会按需挑最合适的一档)
SIZES = [16, 24, 32, 48, 64, 128, 256]


def _trim(img: Image.Image) -> Image.Image:
    """按不透明像素裁到紧贴的正方形(圆形主体不变形, 也不留多余透明边)。

    注意不要"四周留 2% 余量": 源图的圆已经几乎贴边, 再往外扩只会把裁剪框推到
    画布外, 结果反而多出一圈透明边(实测 900px 的图裁成了 930px)。
    """
    bbox = img.split()[-1].getbbox()
    if not bbox:
        return img
    l, t, r, b = bbox
    side = min(max(r - l, b - t), min(img.width, img.height))
    cx, cy = (l + r) // 2, (t + b) // 2
    half = side // 2
    # 夹回画布内, 避免裁剪框越界(越界部分会被填成透明)
    left = max(0, min(cx - half, img.width - side))
    top = max(0, min(cy - half, img.height - side))
    return img.crop((left, top, left + side, top + side))


def _resize(img: Image.Image, size: int) -> Image.Image:
    """逐步减半 + 小尺寸锐化, 让细笔画不糊。"""
    cur = img
    while cur.width > size * 2:
        cur = cur.resize((cur.width // 2, cur.height // 2), Image.LANCZOS)
    cur = cur.resize((size, size), Image.LANCZOS)
    if size <= 32:
        cur = cur.filter(ImageFilter.UnsharpMask(radius=1.0, percent=110,
                                                 threshold=2))
    return cur


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "logo.png"
    if not src.exists():
        print(f"源图不存在: {src}")
        return 1
    img = _trim(Image.open(src).convert("RGBA"))
    OUT.mkdir(parents=True, exist_ok=True)

    frames = [_resize(img, s) for s in SIZES]
    big = frames[-1]
    big.save(OUT / "icon.png")
    # 直接把各尺寸帧交给 PIL 打包成 ico(比单图 + sizes= 参数更可控)
    frames[-1].save(OUT / "icon.ico", format="ICO",
                    sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print(f"源图: {src}  ({img.width}x{img.height} 裁剪后)")
    print(f"已生成: {OUT / 'icon.ico'}")
    print(f"已生成: {OUT / 'icon.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
