"""m3u8 下载引擎 —— 复刻 N_m3u8DL-CLI 核心能力。

功能:
- Master Playlist 自动选择最高码率(或指定变体)
- 并发分片下载 + 失败重试 + 超时控制
- AES-128-CBC 自动解密(IV 取 EXT-X-KEY 或分片序号)
- fMP4(EXT-X-MAP init 分片)支持: init + 分片二进制拼接即合法 MP4
- 二进制合并(TS 直出 / fMP4 直出)或 ffmpeg 合并(remux MP4)
- 范围选择(按 HH:MM:SS 时间)
- 下载限速(KB/s)
- 直播流录制(滚动窗口追加, 停止即完成)
- 进度回调: 分片数 / 字节 / 速度 / ETA, 日志格式对齐 CLI 风格
"""
from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import requests

from . import reqhdr
from .fftools import find_ffmpeg, run_hidden
from .playlist import (
    MediaPlaylist,
    MasterPlaylist,
    Segment,
    format_duration,
    format_size,
    is_placeholder_url,
    parse_m3u8,
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass
class DownloadConfig:
    url: str
    save_dir: str
    save_name: str
    headers: dict = field(default_factory=dict)
    #: 来源页(宿主页)地址。请求头回退策略里有一档要用它做 Referer,
    #: 缺了就只能靠"目标站自身域/不带 Referer"两档。
    page_url: str = ""
    threads: int = 16
    retry: int = 15
    timeout: int = 10
    speed_limit: int = 0            # KB/s, 0 = 不限速
    range_start: str = ""           # HH:MM:SS
    range_end: str = ""
    delete_after_merge: bool = True
    binary_merge: bool = True
    no_merge: bool = False
    use_proxy: bool = True
    variant_index: int = -1         # -1 = 自动最高码率
    # ---- 更多功能（逆向自猫抓 m3u8.js）----
    skip_decrypt: bool = False      # 跳过 AES 解密, 直接保存密文分片
    strip_image: bool = False       # 剥掉分片前的 PNG/JPG 伪装头
    extra_query: str = ""           # 追加到每个分片地址的查询参数(tsAddArg)
    selected: Optional[List[int]] = None   # 只下载这些下标的分片
    audio_only: bool = False        # 仅保留音频轨(需 ffmpeg 合并)
    #: 深度搜索捕获到的 m3u8 原文。有值时不再请求 url, 直接用这段文本解析
    #: (站点把清单存内存里、从不上网的情形)。此时 url 仅作为相对地址基准。
    playlist_text: str = ""
    #: 直链下载(mp4/flv/webm...)。由调用方按地址特征判定后置位;
    #: 为 True 时走 Range 分块并发, 不解析 m3u8。
    is_direct: bool = False
    #: DASH 音轨地址(B站一类)。非空时走 ``_run_dash``: 音视频分别下载,
    #: 再用 ffmpeg ``-c copy`` 合成 mp4 —— 只下 video.m4s 会得到无声视频。
    audio_url: str = ""


class _Throttle:
    """简单限速器: 1 秒滑动窗口内超速则睡眠补齐。"""

    def __init__(self, bytes_per_sec: int):
        self.limit = bytes_per_sec
        self.window_start = time.monotonic()
        self.window_bytes = 0

    def consume(self, n: int) -> None:
        if self.limit <= 0:
            return
        self.window_bytes += n
        elapsed = time.monotonic() - self.window_start
        if elapsed >= 1.0:
            self.window_start = time.monotonic()
            self.window_bytes = n
            return
        expected = self.window_bytes / self.limit
        if expected > elapsed:
            time.sleep(expected - elapsed)


class M3U8Downloader:
    def __init__(self, config: DownloadConfig,
                 on_log: Callable[[str], None],
                 on_progress: Callable[[dict], None],
                 on_state: Callable[[str], None],
                 ffmpeg_path=None):
        self.cfg = config
        self._log = on_log
        self._progress = on_progress
        self._state = on_state
        self.ffmpeg_path = ffmpeg_path
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()             # set = 运行中
        self.session = requests.Session()
        # session 上只放 UA。**不要**把 config.headers 灌进来: 那套头是界面
        # 组装的"候选头", 未必对目标 CDN 有效(含 Accept-Language 会被某些站
        # 直接 403), 而且 requests 会把它合并进每一次请求 —— 一旦灌进来,
        # reqhdr 的 minimal 档也救不回来。正确的头由 _adopt_headers() 在拿到
        # 清单后**实测可用**再整体替换, 之后分片自然复用。
        self.session.headers.update({"User-Agent": DEFAULT_UA})
        if not config.use_proxy:
            self.session.trust_env = False
        self._key_cache = {}
        self._key_iv_hex = ""
        self._media_url = config.url
        self._eff_url = ""            # 最近一次请求重定向后的最终地址
        self._bytes_done = 0
        #: 文件总字节数(已知时用于"整文件进度"; VOD 分片流事先不知道, 记 0)
        self._bytes_total = 0
        self._segs_done = 0
        self._segs_total = 0
        self._speed = 0.0
        self._start_time = 0.0
        self._last_emit_t = 0.0
        self._last_emit_b = 0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 控制接口
    def stop(self):
        self._stop.set()
        self._pause.set()

    def pause(self):
        self._pause.clear()

    def resume(self):
        self._pause.set()

    @property
    def paused(self) -> bool:
        return not self._pause.is_set()

    def _wait_if_paused(self):
        while not self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.2)

    # ---------------------------------------------------------------- 日志
    def _log_line(self, msg: str):
        now = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int((now % 1) * 1000):03d}"
        self._log(f"[{ts}] {msg}")

    # ---------------------------------------------------------------- 主流程
    def run(self) -> str:
        """执行下载, 返回 'done' | 'failed' | 'stopped'。"""
        try:
            self._start_time = time.monotonic()
            self._log_line(f"文件名称: {self.cfg.save_name}")
            self._log_line(f"存储路径: {self.cfg.save_dir}")
            self._log_line(f"开始解析: {self.cfg.url}")
            # 直链(mp4/flv/webm...): 不是 m3u8, 走 Range 分块下载。
            # 常见于 stripchat 系站点 —— 它们下发的是 ffmpeg 风格的 .mp4,
            # 以前会被当 m3u8 解析, 报"无法解析 m3u8 播放列表"。
            # DASH(音视频分离): B站一类。必须走合并流程, 否则成片只有画面
            if self.cfg.audio_url:
                return self._run_dash()
            if self.cfg.is_direct or self.is_direct_url(self.cfg.url):
                return self._run_direct()
            # 深度搜索捕获到内存中的 m3u8 原文时, 不再走网络(该地址可能根本不存在)
            if self.cfg.playlist_text:
                self._log_line("使用深度搜索捕获的 m3u8 原文(不请求网络)")
                pl = parse_m3u8(self.cfg.playlist_text, self.cfg.url)
                if not isinstance(pl, MediaPlaylist):
                    raise RuntimeError("捕获的 m3u8 原文不是媒体列表(可能是 master 名单)")
            else:
                pl = self._resolve_playlist()

            if pl.is_live:
                return self._run_live(pl)
            return self._run_vod(pl)
        except Exception as e:
            self._log_line(f"下载失败: {e}")
            self._state("failed")
            return "failed"

    # ------------------------------------------------------------- 直链下载
    #: 直链视频的扩展名特征。**不含 .ts** —— 那是 m3u8 分片的常见后缀,
    #: 单个分片没有下载意义, 收进来只会污染列表(而且 .m3u8 的变体地址常带
    #: ``?u=...mp4`` 之类的参数, 一并会误判)。
    _DIRECT_RE = re.compile(r"\.(mp4|flv|webm|mkv|mov|m4v)(\?|#|$)", re.I)

    _CTYPE_EXT = {
        "video/mp4": ".mp4", "video/x-flv": ".flv", "video/webm": ".webm",
        "video/quicktime": ".mov", "video/x-matroska": ".mkv",
        "video/mp2t": ".ts", "audio/mp4": ".m4a", "audio/mpeg": ".mp3",
    }

    @classmethod
    def is_direct_url(cls, url: str) -> bool:
        """地址是否指向可直接 HTTP 下载的视频文件(非 m3u8 清单)。

        只看 **path** 部分: m3u8 变体常把原始 mp4 塞在查询参数里
        (``/seg1.ts?u=http://x/a.mp4``), 按整串匹配会误判。
        """
        u = url or ""
        if ".m3u8" in u.lower():
            return False
        try:
            from urllib.parse import urlparse

            path = urlparse(u).path or ""
        except Exception:
            path = u.split("?")[0]
        return bool(cls._DIRECT_RE.search(path))

    @classmethod
    def _ext_from_url(cls, url: str) -> str:
        path = (url or "").split("?")[0].split("#")[0]
        m = re.search(r"\.(mp4|flv|webm|mkv|mov|m4v|ts)$", path, re.I)
        return ("." + m.group(1).lower()) if m else ""

    def _probe_media_url(self, url: str):
        """试探某个直链: 找到能用的请求头, 顺带拿总大小与是否支持 Range。

        细节见 :func:`reqhdr.probe_media` —— 只取 1 字节, 不会把整个文件拉下来;
        同样要走多策略回退(该 CDN 要求 Referer/Origin 是播放器 iframe 自身域)。
        """
        res = reqhdr.probe_media(self.session, url,
                                 dict(self.cfg.headers or {}),
                                 self.cfg.page_url or "",
                                 self.cfg.timeout)
        if not res.ok:
            raise RuntimeError(f"直链探测失败: {res.error or res.status}")
        return res.variant, dict(res.headers), res.total, res.ranges, res.ctype

    def _probe_direct(self):
        """探测主地址(兼容旧调用点)。"""
        return self._probe_media_url(self.cfg.url)

    def _fetch_spans(self, url: str, part: Path, total: int,
                     ranges_ok: bool) -> None:
        """把一个直链按 Range 切成若干块并发下载, 顺序写入 ``part``。

        这是直链下载与 DASH 双流共用的核心: 只负责"把 url 落成 part",
        进度累加到 ``self._bytes_done``(所以调用方要先把 ``_bytes_total``
        设成所有流的字节和, 进度条才不会跳)。
        """
        # 直链是**单个大文件**, 并发开太多会被 CDN 限流(实测 16 路跑到 78%
        # 时整片 503)。6 路已能跑满带宽(实测 50+ MB/s), 多余连接只会招限流。
        if ranges_ok and total > 0:
            live_workers = max(1, min(self.cfg.threads, 6))
        else:
            live_workers = 1

        # 块大小自适应: 让块数大约是线程数的 4 倍, 既不浪费连接也便于并行
        # 收尾。小文件(如 3MB)也能切成多块, 否则"并发"形同虚设。
        if total > 0 and ranges_ok:
            chunk = max(1024 * 1024, total // max(4, self.cfg.threads * 4))
            spans = [(s, min(s + chunk, total) - 1)
                     for s in range(0, total, chunk)]
        else:
            spans = [(0, -1)]

        self._log_line("分段下载: "
                       + (f"支持 · 并发 {live_workers}" if ranges_ok
                          else "不支持(单流下载)"))
        self._segs_total += len(spans)
        self._last_emit_t = 0.0
        self._last_emit_b = 0

        # 文件必须**无条件**先建出来: 单流/不支持 Range 时也要能 open(part,"r+b")。
        # 支持分块时顺便做稀疏预分配, 各线程按各自偏移写入。
        try:
            with open(part, "wb") as f:
                if total > 0 and ranges_ok:
                    f.truncate(total)
        except Exception:
            part.write_bytes(b"")

        throttle = (_Throttle(self.cfg.speed_limit * 1024)
                    if self.cfg.speed_limit > 0 else None)
        lock = threading.Lock()
        errors: List[str] = []
        attempts_max = max(4, min(6, self.cfg.retry))

        def fetch(span) -> None:
            start, end = span
            if self._stop.is_set():
                return
            self._wait_if_paused()
            hh = dict(self.session.headers)
            if end >= 0:
                hh["Range"] = f"bytes={start}-{end}"
            pos = start if end >= 0 else 0
            for attempt in range(attempts_max):
                if self._stop.is_set():
                    return
                got = 0
                try:
                    with self.session.get(
                            url, headers=hh, stream=True,
                            timeout=max(15, self.cfg.timeout)) as r:
                        if r.status_code not in (200, 206):
                            raise RuntimeError(f"HTTP {r.status_code}")
                        fh = open(part, "r+b")
                        try:
                            for buf in r.iter_content(64 * 1024):
                                if self._stop.is_set():
                                    break
                                self._wait_if_paused()
                                if not buf:
                                    continue
                                if throttle is not None:
                                    throttle.consume(len(buf))
                                with lock:
                                    fh.seek(pos)
                                    fh.write(buf)
                                    pos += len(buf)
                                    got += len(buf)
                                    self._bytes_done += len(buf)
                                self._emit_progress()
                        finally:
                            fh.close()
                    if self._stop.is_set():
                        return
                    if end >= 0 and got != end - start + 1:
                        raise RuntimeError(f"分块长度不符({got}/"
                                           f"{end - start + 1})")
                    with lock:
                        self._segs_done += 1
                    self._emit_progress(live=True)
                    return
                except Exception as e:        # noqa: BLE001 - 单块重试
                    if attempt >= attempts_max - 1:
                        with lock:
                            errors.append(f"偏移 {start}: "
                                          f"{type(e).__name__} {e}")
                        return
                    # 503/429 是限流, 指数退避等它缓过来; 其它错误线性退避
                    text = str(e)
                    wait = (0.8 * (2 ** attempt)
                            if ("503" in text or "429" in text
                                or "Timeout" in type(e).__name__)
                            else 0.4 * (attempt + 1))
                    time.sleep(min(6.0, wait))

        # 直链并发上限: 见上方 live_workers 的注释
        workers = live_workers if len(spans) > 1 else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(fetch, s) for s in spans]
            for fut in as_completed(futures):
                if self._stop.is_set():
                    break
                fut.result()

        if errors and not self._stop.is_set():
            tip = ""
            if any("503" in e or "429" in e for e in errors):
                tip = " —— 站点限流, 可在「线程数」里调低后重试"
            raise RuntimeError(
                f"{len(errors)}/{len(spans)} 个分块失败: {errors[0]}{tip}")

    def _run_direct(self) -> str:
        """按 Range 分块并发下载整段视频文件。"""
        url = self.cfg.url
        name, use, total, ranges_ok, ctype = self._probe_direct()
        self._adopt_headers(use, name)

        ext = self._CTYPE_EXT.get(ctype) or self._ext_from_url(url) or ".mp4"
        base_name = re.sub(r"\.(mp4|flv|webm|mkv|mov|m4v|ts|m4a|mp3)$", "",
                           self.cfg.save_name or "video", flags=re.I) or "video"
        out_dir = Path(self.cfg.save_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{base_name}{ext}"
        part = out_dir / f"{base_name}{ext}.part"

        self._log_line(f"识别为直链视频({ctype or '未知类型'}), "
                       f"大小 {format_size(total) if total else '未知'}")
        self._segs_total = 0
        self._segs_done = 0
        self._bytes_done = 0
        self._bytes_total = total if (total > 0 and ranges_ok) else 0
        self._last_emit_t = 0.0
        self._last_emit_b = 0
        self._state("running")
        self._emit_progress(live=True)

        self._fetch_spans(url, part, total, ranges_ok)

        if self._stop.is_set():
            self._log_line(f"已停止下载(未完成文件保留为 {part.name})")
            self._state("stopped")
            return "stopped"

        try:
            if out.exists():
                out.unlink()
            part.replace(out)
        except Exception:                     # noqa: BLE001 - 跨盘/占用兜底
            import shutil
            shutil.move(str(part), str(out))

        self._segs_done = self._segs_total
        self._emit_progress(live=True)
        try:
            size = out.stat().st_size
        except OSError:
            size = total
        self._log_line(f"下载完成: {out.name} ({format_size(size)})")
        self._state("done")
        return "done"

    # ------------------------------------------------------------- DASH 双流
    @staticmethod
    def dash_merge_cmd(ffmpeg, video, audio, out) -> List[str]:
        """DASH 音视频合并命令(纯函数, 便于单测)。

        ``-c copy`` 不重编码, 几秒就能合完; ``-movflags +faststart`` 把 moov
        挪到文件头, 边下边播/进度条拖动才不会卡。
        """
        return [str(Path(ffmpeg).resolve()), "-y", "-hide_banner",
                "-loglevel", "error",
                "-i", str(video), "-i", str(audio),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c", "copy", "-movflags", "+faststart", str(out)]

    def _run_dash(self) -> str:
        """DASH 源(B站等): 视频流 + 音频流分别下载, 再用 ffmpeg 合成 mp4。

        与 HLS 最大的不同: DASH 的 ``video.m4s`` / ``audio.m4s`` 是两条**独立的
        完整文件**, 不是带音轨的 TS 分片。只下视频 = 成片没声音, 所以必须两条
        都下再 ``-c copy`` 合并(不重编码)。进度按两条流的字节总和算, 中途不跳。
        """
        audio_url = self.cfg.audio_url
        ffmpeg = self.ffmpeg_path or find_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError("DASH 视频需要 ffmpeg 合并音视频, 未检测到 ffmpeg")

        out_dir = Path(self.cfg.save_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        base_name = re.sub(r"\.(mp4|flv|webm|mkv|mov|m4v|ts|m4a|mp3)$", "",
                           self.cfg.save_name or "video", flags=re.I) or "video"
        out = out_dir / f"{base_name}.mp4"
        vpart = out_dir / f"{base_name}.video.part"
        apart = out_dir / f"{base_name}.audio.part"

        name, use, v_total, v_ranges, _ = self._probe_media_url(self.cfg.url)
        self._adopt_headers(use, name)
        a_total, a_ranges = 0, False
        if audio_url:
            _, a_use, a_total, a_ranges, _ = self._probe_media_url(audio_url)
            if a_total <= 0 or not a_ranges:
                a_total = 0

        self._log_line(
            f"DASH 双流: 视频 {format_size(v_total) if v_total else '未知'}"
            + (f" + 音频 {format_size(a_total)}" if a_total else " (无音频流)"))

        self._segs_total = 0
        self._segs_done = 0
        self._bytes_done = 0
        self._bytes_total = ((v_total if (v_total > 0 and v_ranges) else 0)
                             + (a_total if a_ranges else 0))
        self._last_emit_t = 0.0
        self._last_emit_b = 0
        self._state("running")
        self._emit_progress(live=True)

        self._log_line("下载视频流…")
        self._fetch_spans(self.cfg.url, vpart, v_total, v_ranges)
        if self._stop.is_set():
            self._log_line(f"已停止下载(未完成文件保留为 {vpart.name})")
            self._state("stopped")
            return "stopped"
        if audio_url:
            self._log_line("下载音频流…")
            self._fetch_spans(audio_url, apart, a_total, a_ranges)
            if self._stop.is_set():
                self._log_line(f"已停止下载(未完成文件保留为 {vpart.name})")
                self._state("stopped")
                return "stopped"

        if audio_url:
            cmd = self.dash_merge_cmd(ffmpeg, vpart, apart, out)
            self._log_line(f"合并音视频: {out.name}")
            proc = run_hidden(cmd, capture_output=True, text=True,
                              timeout=600)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip()[:300]
                raise RuntimeError(f"ffmpeg 合并失败: {err}")
        else:
            # 没有独立音频流(少数源直接把音轨塞在视频文件里): 直接改名
            try:
                if out.exists():
                    out.unlink()
                vpart.replace(out)
            except Exception:                 # noqa: BLE001 - 跨盘兜底
                import shutil
                shutil.move(str(vpart), str(out))

        for p in (vpart, apart):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

        self._segs_done = self._segs_total
        self._emit_progress(live=True)
        try:
            size = out.stat().st_size
        except OSError:
            size = self._bytes_done
        self._log_line(f"下载完成: {out.name} ({format_size(size)})")
        self._state("done")
        return "done"

    def _resolve_playlist(self) -> MediaPlaylist:
        """解析配置地址, 逐层展开 master, 直到拿到媒体列表。

        两道保护都是实测踩出来的(某网盘型源站 uqload):

        1. **用最终地址做 base**: 未获授权时源站不返回 403, 而是 302 到
           ``taunt/promo/master.m3u8`` 这样的"钓鱼清单"。该清单是合法 m3u8,
           变体却是相对地址 ``index.m3u8``; 若拿重定向前地址做 base, 变体会
           解析回原地址 → "master → 变体 → master" 死循环, 最终报出完全
           误导的"无法解析 m3u8 播放列表"。
        2. **自引用保护**: 变体地址等于已请求过的地址时立刻停下并说明原因,
           而不是继续转圈。
        """
        url = self.cfg.url
        seen = set()
        for depth in range(6):
            if url in seen:
                raise RuntimeError(
                    "master playlist 的变体指向已用过的地址(占位/广告流特征), "
                    "已停止解析")
            seen.add(url)
            text = self._fetch_text(url)
            final = self._eff_url or url
            if is_placeholder_url(final):
                raise RuntimeError(
                    "地址被重定向到占位流(taunt/promo): 当前地址未获授权或 token "
                    f"已过期 —— {final}")
            if final != url:
                self._log_line(f"地址已重定向: {final[:100]}")
            pl = parse_m3u8(text, final)

            if isinstance(pl, MediaPlaylist):
                return pl
            if not isinstance(pl, MasterPlaylist):
                raise RuntimeError("无法解析 m3u8 播放列表")
            if not pl.variants:
                raise RuntimeError("master playlist 里没有可用变体")

            self._log_line(f"读取到大师列表(master playlist), 变体 {len(pl.variants)} 个")
            idx = self.cfg.variant_index if depth == 0 else -1
            if idx < 0 or idx >= len(pl.variants):
                idx = max(range(len(pl.variants)),
                          key=lambda i: pl.variants[i].bandwidth)
            variant = pl.variants[idx]
            self._log_line(f"已选择清晰度: {variant.label}")
            if variant.url == final or variant.url in seen:
                raise RuntimeError(
                    "master playlist 的变体指向自身(占位/广告流特征)。"
                    "这通常意味着 token 已失效: 请在「浏览器嗅探」页签重新播放"
                    "一次, 再解析捕获到的新地址")
            self._media_url = variant.url
            self._log_line(f"重新解析: {variant.url}")
            url = variant.url
        raise RuntimeError("master playlist 嵌套层数过多(>6), 已停止解析")

    # ---------------------------------------------------------------- 网络
    def _fetch_text(self, url: str, referer: Optional[str] = None) -> str:
        """拉取清单文本（带请求头回退 + 瞬断重试）。

        站点对 Referer 的要求互相矛盾（有的要宿主页、有的要自身域、有的不能带），
        所以走 ``reqhdr`` 的多策略回退；命中后把**实测可用的那套头**写回
        ``self.session``，保证后续分片请求用同一套（否则清单能取、分片被 403）。
        """
        base = dict(self.cfg.headers or {})
        if referer:
            base["Referer"] = referer
        res = reqhdr.fetch_playlist(self.session, url, base,
                                    self.cfg.page_url or "", self.cfg.timeout,
                                    self._log_line)
        if not res.ok:
            raise RuntimeError(reqhdr.describe_failure(res) or "清单获取失败")
        # 记录重定向后的最终地址: 相对地址必须相对它解析
        self._eff_url = res.final_url or url
        self._adopt_headers(res.headers, res.variant)
        return res.text

    def _adopt_headers(self, headers, variant: str = ""):
        """把实测可用的那套头**整体替换**到 session 上。

        必须是 replace 而不是 ``update``: 该 CDN 做"头集合指纹"校验, 多一个
        ``Accept-Language`` 就 403; 而 ``update`` 只会覆盖同名键, 删不掉构造
        session 时写进去的脏头 —— 结果清单能取、分片全被拒。
        """
        use = {k: str(v) for k, v in (headers or {}).items() if k and v}
        if not use:
            return
        if not any(k.lower() == "user-agent" for k in use):
            use["User-Agent"] = DEFAULT_UA
        try:
            self.session.headers.clear()
            for k, v in use.items():
                self.session.headers[k] = v
        except Exception:                       # noqa: BLE001 - 极端兜底
            self.session.headers.update(use)
        if variant and variant != "captured":
            self._log_line(f"清单请求头改用「{variant}」策略(供分片复用)")

    def _seg_url(self, url: str) -> str:
        """分片地址追加自定义查询参数（对应猫抓的 tsAddArg）。"""
        if not self.cfg.extra_query:
            return url
        from .fragtools import append_query_params

        return append_query_params(url, self.cfg.extra_query)

    def _prepare(self, data: bytes) -> bytes:
        """下载后、解密前的数据预处理（对应猫抓的 dataPreprocessing）。"""
        if not self.cfg.strip_image:
            return data
        from .fragtools import strip_image_wrapper

        return strip_image_wrapper(data)

    def _fetch_bytes(self, url: str) -> bytes:
        last_err = None
        for attempt in range(max(1, self.cfg.retry)):
            if self._stop.is_set():
                raise RuntimeError("任务已停止")
            try:
                r = self.session.get(url, timeout=self.cfg.timeout)
                r.raise_for_status()
                return r.content
            except Exception as e:
                last_err = e
                if attempt < self.cfg.retry - 1:
                    time.sleep(min(2.0, 0.3 * (attempt + 1)))
        raise RuntimeError(f"分片请求失败({url[:80]}): {last_err}")

    # ---------------------------------------------------------------- AES 解密
    def _load_key(self, pl: MediaPlaylist) -> Optional[bytes]:
        key = pl.key
        if not key or key.method != "AES-128" or not key.uri:
            return None
        if key.uri in self._key_cache:
            return self._key_cache[key.uri]
        self._key_iv_hex = key.iv.replace("0x", "").replace("0X", "") if key.iv else ""
        data = self._fetch_bytes(key.uri)
        if len(data) != 16:
            raise RuntimeError(f"AES-128 KEY 长度异常: {len(data)} 字节")
        self._key_cache[key.uri] = data
        return data

    def _iv_for(self, abs_index: int) -> bytes:
        """IV 优先取 EXT-X-KEY 的 IV 属性, 否则按 HLS 规范取分片绝对序号。"""
        if self._key_iv_hex:
            try:
                return bytes.fromhex(self._key_iv_hex)
            except ValueError:
                pass
        return abs_index.to_bytes(16, "big")

    def _decrypt(self, data: bytes, key: bytes, abs_index: int) -> bytes:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7

        cipher = Cipher(algorithms.AES(key), modes.CBC(self._iv_for(abs_index)))
        dec = cipher.decryptor()
        out = dec.update(data) + dec.finalize()
        try:
            unpadder = PKCS7(128).unpadder()
            return unpadder.update(out) + unpadder.finalize()
        except ValueError:
            return out

    # ---------------------------------------------------------------- 范围选择
    @staticmethod
    def _parse_time(s: str) -> float:
        parts = s.strip().split(":")
        try:
            vals = [float(p) for p in parts]
        except ValueError:
            return 0.0
        if len(vals) == 3:
            return vals[0] * 3600 + vals[1] * 60 + vals[2]
        if len(vals) == 2:
            return vals[0] * 60 + vals[1]
        return vals[0]

    def _resolve_range(self, pl: MediaPlaylist):
        n = len(pl.segments)
        start_i, end_i = 0, n - 1
        if self.cfg.range_start:
            t = self._parse_time(self.cfg.range_start)
            acc = 0.0
            for i, seg in enumerate(pl.segments):
                if acc + seg.duration > t:
                    start_i = i
                    break
                acc += seg.duration
        if self.cfg.range_end:
            t = self._parse_time(self.cfg.range_end)
            acc = 0.0
            for i, seg in enumerate(pl.segments):
                acc += seg.duration
                if acc >= t:
                    end_i = i
                    break
        return start_i, min(end_i, n - 1)

    # ---------------------------------------------------------------- 进度
    def _emit_progress(self, live: bool = False):
        now = time.monotonic()
        dt = now - self._last_emit_t
        if dt < 0.25 and not live:
            return
        db = self._bytes_done - self._last_emit_b
        inst = db / dt if dt > 0 else 0.0
        self._speed = inst if self._speed == 0 else self._speed * 0.7 + inst * 0.3
        self._last_emit_t = now
        self._last_emit_b = self._bytes_done
        elapsed = now - self._start_time
        pct = (self._segs_done / self._segs_total * 100) if self._segs_total else 0.0
        eta = 0.0
        if self._segs_done > 0 and self._segs_total and self._speed > 0:
            avg = self._bytes_done / self._segs_done
            remain = (self._segs_total - self._segs_done) * avg
            eta = remain / self._speed
        self._progress({
            "state": "running",
            "live": live,
            "done": self._segs_done,
            "total": self._segs_total,
            "percent": pct,
            "bytes_done": self._bytes_done,
            "bytes_total": self._bytes_total,
            "speed": self._speed,
            "eta": eta,
            "elapsed": elapsed,
        })

    # ---------------------------------------------------------------- VOD
    def _run_vod(self, pl: MediaPlaylist) -> str:
        segments = pl.segments
        if self.cfg.selected is not None:
            keep = set(self.cfg.selected)
            before = len(segments)
            segments = [s for i, s in enumerate(segments) if i in keep]
            self._log_line(f"勾选筛选: {before} -> {len(segments)} 个分片")
            if not segments:
                self._log_line("未勾选任何分片, 已中止")
                self._state("stopped")
                return "stopped"
        start_i, end_i = self._resolve_range(pl)
        if start_i > 0 or end_i < len(pl.segments) - 1:
            if self.cfg.selected is None:
                segments = segments[start_i:end_i + 1]
            self._log_line(f"范围选择: 分片 {start_i + 1} - {end_i + 1}")
        self._segs_total = len(segments)
        self._log_line(f"总时长: {format_duration(pl.duration)}")
        self._log_line(f"总分片: {len(pl.segments)}, 已选择分片: {self._segs_total}")
        if self.cfg.extra_query:
            self._log_line(f"分片参数追加: ?{self.cfg.extra_query.lstrip('?')}")
        if self.cfg.strip_image:
            self._log_line("数据预处理: 开启(剥离 PNG/JPG 伪装头)")
        if self.cfg.skip_decrypt:
            self._log_line("跳过解密: 开启(保存密文分片)")
        if pl.key and pl.key.method == "AES-128" and not self.cfg.skip_decrypt:
            self._log_line("检测到 AES-128 加密, 将自动解密")
        if pl.init_url:
            self._log_line("检测到 fMP4 分片(EXT-X-MAP)")
        self._log_line("开始下载分片...")
        self._state("running")

        work_dir = Path(self.cfg.save_dir).resolve() / f".m3u8dl_{self.cfg.save_name}"
        work_dir.mkdir(parents=True, exist_ok=True)
        for f in work_dir.glob("seg_*"):
            try:
                f.unlink()
            except OSError:
                pass

        key_bytes = self._load_key(pl) if pl.key else None
        init_bytes = b""
        if pl.init_url:
            try:
                init_bytes = self._fetch_bytes(pl.init_url)
                self._log_line(f"init 分片: {format_size(len(init_bytes))}")
            except Exception as e:
                self._log_line(f"init 分片获取失败: {e}")

        throttle = _Throttle(self.cfg.speed_limit * 1024) if self.cfg.speed_limit > 0 else None

        def download_one(i: int, seg: Segment):
            if self._stop.is_set():
                return
            self._wait_if_paused()
            try:
                data = self._prepare(self._fetch_bytes(self._seg_url(seg.url)))
                if key_bytes and not self.cfg.skip_decrypt:
                    data = self._decrypt(data, key_bytes, pl.media_sequence + seg.index)
                out = work_dir / f"seg_{i:06d}.bin"
                out.write_bytes(data)
                with self._lock:
                    self._segs_done += 1
                    self._bytes_done += len(data)
                    if throttle:
                        throttle.consume(len(data))
                    self._emit_progress()
            except Exception as e:
                self._log_line(f"分片 {i} 下载失败: {e}")

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.threads)) as pool:
            futures = [pool.submit(download_one, i, seg) for i, seg in enumerate(segments)]
            for _ in as_completed(futures):
                if self._stop.is_set():
                    break

        if self._stop.is_set():
            self._log_line("下载已停止")
            self._state("stopped")
            self._progress({"state": "stopped"})
            return "stopped"

        # 补漏: 对缺失分片做一轮串行重试
        missing = [i for i in range(self._segs_total)
                   if not (work_dir / f"seg_{i:06d}.bin").exists()]
        if missing:
            self._log_line(f"{len(missing)} 个分片缺失, 补漏重试…")
            for i in missing:
                if self._stop.is_set():
                    break
                self._wait_if_paused()
                try:
                    data = self._prepare(
                        self._fetch_bytes(self._seg_url(segments[i].url)))
                    if key_bytes and not self.cfg.skip_decrypt:
                        data = self._decrypt(data, key_bytes,
                                             pl.media_sequence + segments[i].index)
                    (work_dir / f"seg_{i:06d}.bin").write_bytes(data)
                    with self._lock:
                        self._segs_done += 1
                        self._bytes_done += len(data)
                except Exception as e:
                    self._log_line(f"分片 {i} 补漏失败: {e}")

        self._log_line(f"Progress: {self._segs_done}/{self._segs_total} "
                       f"({self._segs_done / max(1, self._segs_total) * 100:.2f}%)")
        self._log_line("等待下载完成...")
        return self._merge_vod(work_dir, segments, init_bytes)

    # ---------------------------------------------------------------- 直播
    def _run_live(self, pl: MediaPlaylist) -> str:
        self._log_line("检测到直播流, 进入录制模式(停止即完成)")
        self._state("running")
        out_dir = Path(self.cfg.save_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        is_fmp4 = bool(pl.init_url)
        ext = "mp4" if is_fmp4 else "ts"
        raw = out_dir / f"{self.cfg.save_name}.{ext}"

        key_bytes = self._load_key(pl) if pl.key else None
        init_bytes = b""
        if is_fmp4:
            try:
                init_bytes = self._fetch_bytes(pl.init_url)
            except Exception as e:
                self._log_line(f"init 分片获取失败: {e}")
        cur_key_uri = pl.key.uri if pl.key else ""

        seen = set()
        seg_count = 0
        total_bytes = 0
        refresh = max(1.0, pl.target_duration / 2 if pl.target_duration else 3.0)
        self._segs_total = 0

        with open(raw, "wb") as out:
            if init_bytes:
                out.write(init_bytes)
                total_bytes += len(init_bytes)
            while not self._stop.is_set():
                self._wait_if_paused()
                try:
                    text = self._fetch_text(self._media_url)
                    new_pl = parse_m3u8(text, self._media_url)
                except Exception as e:
                    self._log_line(f"刷新播放列表失败: {e}")
                    time.sleep(refresh)
                    continue
                if not isinstance(new_pl, MediaPlaylist):
                    break
                # KEY 轮换
                if new_pl.key and new_pl.key.uri and new_pl.key.uri != cur_key_uri:
                    cur_key_uri = new_pl.key.uri
                    key_bytes = self._load_key(new_pl)
                    self._log_line("检测到 KEY 轮换, 已更新")
                new_segs = [s for s in new_pl.segments if s.url not in seen]
                for s in new_segs:
                    if self._stop.is_set():
                        break
                    self._wait_if_paused()
                    try:
                        data = self._prepare(self._fetch_bytes(self._seg_url(s.url)))
                        if key_bytes and not self.cfg.skip_decrypt:
                            data = self._decrypt(data, key_bytes,
                                                 new_pl.media_sequence + s.index)
                        out.write(data)
                        out.flush()
                        seen.add(s.url)
                        seg_count += 1
                        total_bytes += len(data)
                        self._segs_done = seg_count
                        self._bytes_done = total_bytes
                        self._emit_progress(live=True)
                    except Exception as e:
                        self._log_line(f"分片下载失败: {e}")
                if len(seen) > 1000:
                    seen = set(s.url for s in new_pl.segments)
                if not new_pl.is_live and not new_segs:
                    break
                time.sleep(refresh)

        self._log_line(f"直播录制结束: {seg_count} 个分片, {format_size(total_bytes)}")
        self._log_line(f"录制文件: {raw}")
        self._state("done")
        self._progress({"state": "done", "path": str(raw)})
        return "done"

    # ---------------------------------------------------------------- 合并
    def _merge_vod(self, work_dir: Path, segments: List[Segment],
                   init_bytes: bytes) -> str:
        if self.cfg.no_merge:
            self._log_line("已禁用自动合并, 分片保留在临时目录")
            self._log_line(f"分片目录: {work_dir}")
            self._state("done")
            self._progress({"state": "done", "path": str(work_dir)})
            return "done"

        self._log_line("开始合并...")
        out_dir = Path(self.cfg.save_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        name = self.cfg.save_name
        all_ts = all(s.url.lower().split("?")[0].endswith(".ts") for s in segments)
        ffmpeg = self.ffmpeg_path or find_ffmpeg()

        # 仅音频必须过 ffmpeg(要丢轨), 二进制合并做不到
        want_audio_only = bool(self.cfg.audio_only) and ffmpeg is not None
        if self.cfg.audio_only and not ffmpeg:
            self._log_line("提示: 仅音频需要 ffmpeg, 未检测到, 将输出完整音视频")

        try:
            if want_audio_only:
                out = out_dir / f"{name}.m4a"
                self._ffmpeg_merge(work_dir, out, ffmpeg, audio_only=True)
            elif init_bytes:
                # fMP4: init + 分片二进制拼接即合法 MP4
                out = out_dir / f"{name}.mp4"
                self._binary_merge(work_dir, out, init_bytes=init_bytes)
            elif all_ts and self.cfg.binary_merge:
                out = out_dir / f"{name}.ts"
                self._binary_merge(work_dir, out)
            elif ffmpeg:
                out = out_dir / f"{name}.mp4"
                self._ffmpeg_merge(work_dir, out, ffmpeg)
            else:
                out = out_dir / f"{name}.ts"
                self._binary_merge(work_dir, out)
                self._log_line("提示: 未检测到 ffmpeg, 已输出 .ts 文件")
        except Exception as e:
            self._log_line(f"合并失败: {e}")
            self._state("failed")
            return "failed"

        if self.cfg.delete_after_merge:
            try:
                for f in work_dir.glob("seg_*"):
                    f.unlink()
                list_file = work_dir / "concat_list.txt"
                if list_file.exists():
                    list_file.unlink()
                work_dir.rmdir()
            except OSError:
                pass
        self._log_line(f"下载完成: {out}")
        self._state("done")
        self._progress({"state": "done", "path": str(out)})
        return "done"

    def _binary_merge(self, work_dir: Path, out: Path, init_bytes: bytes = b""):
        files = sorted(work_dir.glob("seg_*.bin"))
        with open(out, "wb") as w:
            if init_bytes:
                w.write(init_bytes)
            for f in files:
                w.write(f.read_bytes())
        self._log_line(f"二进制合并完成: {out.name} ({format_size(out.stat().st_size)})")

    def _ffmpeg_merge(self, work_dir: Path, out: Path, ffmpeg: Path,
                      audio_only: bool = False):
        files = sorted(work_dir.glob("seg_*.bin"))
        list_file = work_dir / "concat_list.txt"
        # concat 分离器相对列表文件目录解析相对路径, 必须写绝对路径
        with open(list_file, "w", encoding="utf-8") as f:
            for p in files:
                f.write(f"file '{p.resolve().as_posix()}'\n")
        cmd = [str(Path(ffmpeg).resolve()), "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", str(list_file.resolve())]
        if audio_only:
            # 对应猫抓 #onlyAudio: 丢弃视频轨
            cmd += ["-vn"]
        cmd += ["-c", "copy", "-movflags", "+faststart", str(out.resolve())]
        self._log_line(f"调用 ffmpeg 合并{'(仅音频)' if audio_only else ''}: {out.name}")
        proc = run_hidden(cmd, capture_output=True)
        if proc.returncode != 0 or not out.exists():
            err = proc.stderr.decode("utf-8", "ignore")[-300:]
            raise RuntimeError(f"ffmpeg 合并失败: {err}")
        self._log_line(f"ffmpeg 合并完成: {out.name} ({format_size(out.stat().st_size)})")
