"""诊断脚本: 拿 .probe 里落盘的捕获结果, 走一遍软件的真实链路。

链路: 捕获头(经 _build_headers 白名单过滤) -> reqhdr 四档回退 -> parse_m3u8
      -> 变体 -> 分片 -> 密钥

用途: 页面里播放器自己能报错(如 uqload 232011), 但只要链路通就说明
      "下载得到"; 反之能定位卡在哪一层。

用法:
    python tests/probe_chain.py [.probe/xxx.json] [--all]
(不带参数则用最新一份; --all 逐条测所有 master 候选)
"""
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 要 import MainWindow(只为调 _build_headers), 无窗环境需 offscreen
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import requests  # noqa: E402

from m3u8dl import reqhdr  # noqa: E402
from m3u8dl.playlist import (  # noqa: E402
    MasterPlaylist,
    MediaPlaylist,
    parse_m3u8,
)

ROOT = Path(__file__).resolve().parent.parent

# 与 main_window._REPLAY_HEADERS 保持一致
REPLAY = ("referer", "origin", "user-agent", "accept", "accept-language",
          "authorization", "cookie", "x-requested-with")

# 与 main_window 的 defaults 保持一致(含那个会触发 403 的 Accept-Language)
DEFAULT_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/140.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def build_headers(item, defaults=None):
    """**直接调用 MainWindow._build_headers**, 而不是复刻逻辑。

    这样验证的是真实跑在软件里的那段代码(含白名单过滤、Referer 优先、
    不继承 defaults 的浏览器特征头)。QT_QPA_PLATFORM=offscreen 下可无窗运行。
    """
    from m3u8dl.ui.main_window import MainWindow

    defaults = defaults if defaults is not None else DEFAULT_HEADERS

    class _Fake:
        _REPLAY_HEADERS = MainWindow._REPLAY_HEADERS
        _cookies = None

    return MainWindow._build_headers(_Fake(), item, defaults)


def walk(session, url, headers, page_url, label="", quiet=False):
    """从 url 出发走完 清单->变体->首片, 返回 (ok, 说明)。"""
    say = (lambda m: None) if quiet else (lambda m: print(f"  log: {m}"))
    res = reqhdr.fetch_playlist(session, url, headers, page_url, 20, log=say)
    print(f"  清单 ok={res.ok} status={res.status} variant={res.variant} "
          f"retries={res.retries}")
    if not res.ok:
        print(f"  !! 失败: {reqhdr.describe_failure(res)}")
        print(f"     final={res.final_url[:140]}")
        return False, reqhdr.describe_failure(res)

    pl = parse_m3u8(res.text, res.final_url or url)
    use = dict(res.headers)
    final = res.final_url or url

    if isinstance(pl, MasterPlaylist):
        print(f"  变体数={len(pl.variants)}")
        if not pl.variants:
            print("  !! master 没有变体")
            return False, "master 无变体"
        v = max(pl.variants, key=lambda x: x.bandwidth or 0)
        print(f"  选中: {v.label}  {v.url[:110]}")
        res2 = reqhdr.fetch_playlist(session, v.url, use, final, 20, log=say)
        if not res2.ok:
            print(f"  !! 变体失败: {reqhdr.describe_failure(res2)}")
            return False, "变体: " + reqhdr.describe_failure(res2)
        pl = parse_m3u8(res2.text, res2.final_url or v.url)
        use = dict(res2.headers)
        final = res2.final_url or v.url

    if not isinstance(pl, MediaPlaylist) or not pl.segments:
        print("  !! 没有分片")
        return False, "无分片"

    print(f"  分片={len(pl.segments)} 时长={pl.duration:.0f}s "
          f"直播={pl.is_live} key={'有' if pl.key else '无'}")

    s0 = pl.segments[0]
    try:
        r = session.get(s0.url, headers={**use, "Range": "bytes=0-2047"},
                        timeout=25)
        body = r.content or b""
        print(f"  首片 status={r.status_code} bytes={len(body)} "
              f"head={body[:8].hex()} TS同步={bool(body) and body[0] == 0x47}")
        if r.status_code not in (200, 206):
            return False, f"分片 HTTP {r.status_code}"
    except Exception as e:
        print(f"  !! 分片异常: {type(e).__name__}: {e}")
        return False, f"分片 {type(e).__name__}"

    if pl.key and pl.key.uri:
        try:
            r = session.get(pl.key.uri, headers=use, timeout=20)
            print(f"  密钥 status={r.status_code} bytes={len(r.content)} "
                  f"({pl.key.method})")
        except Exception as e:
            print(f"  !! 密钥异常: {type(e).__name__}: {e}")

    return True, "OK"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    all_mode = "--all" in sys.argv

    p = (Path(args[0]) if args else
         Path(sorted(glob.glob(str(ROOT / ".probe" / "uqload_*.json")))[-1]))
    data = json.loads(p.read_text(encoding="utf-8"))
    hits = data.get("hits") or []
    print(f"[probe] {p.name}  捕获 {len(hits)} 条\n")

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_HEADERS["User-Agent"]})

    m3u8 = [h for h in hits if ".m3u8" in h["url"].lower()]
    masters = [h for h in m3u8 if "master" in h["url"].lower()]
    picks = masters if (all_mode and masters) else (masters or m3u8)[-1:]

    ok_n = 0
    for i, target in enumerate(picks, 1):
        print(f"\n===== [{i}/{len(picks)}] {target['url'][:130]} =====")
        h = build_headers(target)
        print(f"  参考: referer={target['referer'][:80]}")
        print(f"  Referer头={h.get('Referer', '(无)')[:80]}  "
              f"Origin={h.get('Origin', '(无)')[:60]}")
        ok, why = walk(session, target["url"], h, target["referer"])
        ok_n += bool(ok)
        print(f"  => {'OK' if ok else 'FAIL: ' + why}")

    print(f"\n合计: {ok_n}/{len(picks)} 可用")
    return 0 if ok_n == len(picks) else 1


if __name__ == "__main__":
    sys.exit(main())
