"""验证「系统浏览器嗅探」(CDP) 能抓到真实页面的视频请求。

会在本机启动 Edge(独立 profile), 打开指定页面, 20 秒后停止并打印结果。
用户可手动在浏览器里点播放 —— 那样最接近真实使用。

用法:
    python tests/probe_cdp.py [页面地址]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from m3u8dl.cdp import BrowserSniffer, find_browser  # noqa: E402

PAGE = (sys.argv[1] if len(sys.argv) > 1
        else "https://livecamrips.to/video/16401088")
SECONDS = 22


def main():
    app = QApplication(sys.argv)
    name, exe = find_browser()
    print(f"[browser] {name} {exe or '(未找到)'}")
    if not exe:
        return 1

    sn = BrowserSniffer()

    def on_status(msg):
        print(f"[status] {msg}", flush=True)

    def on_hit(item):
        print(f"\n[hit] {item['url'][:130]}")
        for k, v in sorted(item["headers"].items()):
            if k.lower() in ("referer", "origin", "user-agent", "cookie"):
                print(f"    {k}: {str(v)[:110]}")

    sn.status.connect(on_status)
    sn.captured.connect(on_hit)

    def done():
        hits = sn.hits
        print(f"\n=== 共 {len(hits)} 条候选 ===")
        for h in hits:
            print("-", h["type"], h["url"][:120])
        sn.stop(close_browser=True)
        out = Path(__file__).resolve().parent.parent / ".probe"
        out.mkdir(exist_ok=True)
        p = out / f"cdp_{int(time.time())}.json"
        p.write_text(json.dumps(hits, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        print(f"[saved] {p}")
        app.quit()

    sn.status.connect(lambda m: print(f"[log] {m}", flush=True))
    if not sn.start(PAGE):
        print("启动失败")
        return 1
    QTimer.singleShot(SECONDS * 1000, done)
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
