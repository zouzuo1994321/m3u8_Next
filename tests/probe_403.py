"""诊断脚本: 精确定位 streambucket 的 403 触发条件。

现象: 用 requests.get 发「原样捕获头」-> 200; 用 requests.Session(带默认头)
      发同样的头 -> 403。必须找出到底是哪个头/哪一层导致被拒。

用法: python tests/probe_403.py [.probe/xxx.json]
"""
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def get(url, headers, session=None, tag=""):
    try:
        if session is not None:
            r = session.get(url, headers=headers, timeout=20,
                            allow_redirects=False)
        else:
            r = requests.get(url, headers=headers, timeout=20,
                             allow_redirects=False)
        mark = "OK" if r.status_code == 200 else ""
        print(f"  {tag:38s} -> {r.status_code} {mark}")
        return r.status_code
    except Exception as e:
        print(f"  {tag:38s} -> ERR {type(e).__name__}: {str(e)[:60]}")
        return -1


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    p = (Path(args[0]) if args else
         Path(sorted(glob.glob(str(ROOT / ".probe" / "uqload_*.json")))[-1]))
    data = json.loads(p.read_text(encoding="utf-8"))
    hits = [h for h in data["hits"] if ".m3u8" in h["url"].lower()]
    masters = [h for h in hits if "master" in h["url"].lower()] or hits
    target = masters[-1]
    url = target["url"]
    cap = dict(target["headers"])
    cap.pop("Cookie", None)

    print(f"[url] {url[:120]}")
    print(f"[捕获头] {sorted(cap)}")
    print()

    print("=== 基线: 原样捕获头 ===")
    get(url, cap, None, "1 requests.get(捕获头)")

    print("\n=== 加 requests 默认头 ===")
    base = dict(cap)
    base.setdefault("Accept-Encoding", "gzip, deflate")
    base.setdefault("Connection", "keep-alive")
    get(url, base, None, "2 +Accept-Encoding/Connection")

    print("\n=== Session(含默认头) ===")
    s = requests.Session()
    s.headers.update({
        "User-Agent": cap.get("User-Agent", "Mozilla/5.0"),
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    get(url, cap, s, "3 session.get(捕获头)")
    print(f"     实际发送头: {sorted(s.headers)}")

    print("\n=== 逐个剔除, 看哪个头是必需的 ===")
    for drop in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
                 "Origin", "Referer", "Accept", "User-Agent"):
        h = {k: v for k, v in cap.items() if k.lower() != drop.lower()}
        get(url, h, None, f"4 去掉 {drop}")

    print("\n=== 逐个单独加, 看哪个头触发 403 ===")
    core = {k: v for k, v in cap.items() if k.lower() in
            ("user-agent", "referer", "origin")}
    get(url, core, None, "5 仅 UA+Referer+Origin")
    for add in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
                "Accept"):
        h = dict(core)
        if add in cap:
            h[add] = cap[add]
        get(url, h, None, f"6 core + {add}")

    print("\n=== 逐个单独加 (对 Session) ===")
    s2 = requests.Session()
    get(url, core, s2, "7 session.get(core)")
    for add in ("Accept-Language", "Accept-Encoding", "Accept"):
        h = dict(core)
        if add == "Accept-Language":
            h[add] = "zh-CN,zh;q=0.9,en;q=0.8"
        elif add == "Accept-Encoding":
            h[add] = "gzip, deflate"
        else:
            h[add] = "*/*"
        get(url, h, None, f"8 core + {add}")


if __name__ == "__main__":
    main()
