"""从 EasyList / EasyList China 生成内置广告域名清单。

GetAdBlock、uBlock Origin、Adblock Plus 用的都是这套规则源
(https://easylist.to/)。QtWebEngine 装不了浏览器扩展, 所以这里把规则
**移植**成域名集合, 由 m3u8dl/adblock.py 在网络层直接 block。

只提取 ``||domain^`` 形式的**纯域名**规则:
- 带路径的规则(``||x.com/ads/*``)数量巨大且收益低, 跳过;
- 例外规则(``@@||x.com^``)必须排除, 否则会把白名单域名当成广告;
- 形如 ``||x.com^$third-party`` 的保留 —— 域名本身就是广告源。

用法:
    python tools/build_adlist.py            # 下载并生成
    python tools/build_adlist.py --check    # 只统计, 不写文件
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "m3u8dl" / "adlist.py"

SOURCES = (
    ("EasyList", "https://easylist.to/easylist/easylist.txt"),
    ("EasyList China",
     "https://easylist-downloads.adblockplus.org/easylistchina.txt"),
)

# ||example.com^  或  ||example.com^$third-party
_DOMAIN_RE = re.compile(r"^\|\|([a-z0-9][a-z0-9.\-]{2,80})\^(\$.*)?$", re.I)
# 明显的非广告域名(视频/常见 CDN) 白名单, 防止误杀
_NEVER = (
    "googlevideo.com", "ytimg.com", "akamaihd.net", "cloudfront.net",
    "fastly.net", "jsdelivr.net", "unpkg.com", "bootstrapcdn.com",
)


def fetch(url: str, attempts: int = 4) -> str:
    """下载规则文件。easylist.to 会偶发 SSL 瞬断, 必须重试。"""
    last = None
    for i in range(attempts):
        try:
            r = requests.get(
                url, timeout=60,
                headers={"User-Agent": "Mozilla/5.0 (adlist-builder)"})
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = "utf-8"
            return r.text
        except Exception as e:                 # noqa: BLE001
            last = e
            if i < attempts - 1:
                time.sleep(0.8 * (i + 1))
    raise last


def extract(text: str) -> set:
    out = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("!", "#", "[")):
            continue
        if line.startswith("@@"):          # 例外规则: 绝不能收
            continue
        m = _DOMAIN_RE.match(line)
        if not m:
            continue
        host = m.group(1).lower().strip(".")
        if host.startswith("www."):
            host = host[4:]
        if not host or host.count(".") < 1:
            continue
        if any(host == n or host.endswith("." + n) for n in _NEVER):
            continue
        out.add(host)
    return out


def main() -> int:
    check = "--check" in sys.argv
    domains: set = set()
    for name, url in SOURCES:
        try:
            text = fetch(url)
        except Exception as e:                     # noqa: BLE001
            print(f"[skip] {name}: {type(e).__name__}: {e}")
            continue
        got = extract(text)
        print(f"[{name}] {len(text.splitlines())} 行 -> {len(got)} 个域名")
        domains |= got

    print(f"\n合计 {len(domains)} 个域名")
    if check:
        sample = sorted(domains)[:10]
        print("样例:", sample)
        return 0

    body = "\n".join('    "%s",' % d for d in sorted(domains))
    src = (
        '"""内置广告域名清单 —— **自动生成, 请勿手改**。\n\n'
        "来源: EasyList + EasyList China (https://easylist.to/)\n"
        "生成: python tools/build_adlist.py\n"
        f"时间: {time.strftime('%Y-%m-%d %H:%M')}\n"
        f"数量: {len(domains)}\n\n"
        "GetAdBlock / uBlock Origin 用的就是这套规则源; QtWebEngine 装不了\n"
        "浏览器扩展, 所以这里把它移植成域名集合, 由 adblock.py 在网络层拦截。\n"
        '"""\n'
        "from __future__ import annotations\n\n"
        "AD_DOMAINS = frozenset((\n"
        f"{body}\n"
        "))\n"
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(src, encoding="utf-8")
    print(f"已写入 {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
