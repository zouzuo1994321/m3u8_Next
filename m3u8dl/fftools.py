"""ffmpeg 探测与合并辅助。

查找顺序: 打包内置 -> 系统 PATH -> 常见安装目录。
未找到时由界面提供官方下载指引。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

FFMPEG_DOWNLOAD_PAGE = "https://www.gyan.dev/ffmpeg/builds/"
FFMPEG_GITHUB_PAGE = "https://github.com/BtbN/FFmpeg-Builds/releases"

#: Windows 下"不创建控制台窗口"的标志。
#: 打包成 --noconsole 的 exe 之后, 任何 subprocess 调用(哪怕只是
#: ``ffmpeg -version``)都会**闪一个黑色控制台窗口** —— 用户看到的就是
#: "软件打开的时候有个黑色弹窗"。所有外部命令都必须带上它。
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def run_hidden(cmd, **kwargs):
    """``subprocess.run`` 的封装: Windows 下不弹黑色控制台窗口。"""
    if NO_WINDOW:
        kwargs.setdefault("creationflags", NO_WINDOW)
    return subprocess.run(cmd, **kwargs)

_COMMON_DIRS = [
    Path(r"C:\ffmpeg\bin"),
    Path(r"C:\Program Files\ffmpeg\bin"),
    Path(r"C:\Program Files (x86)\ffmpeg\bin"),
]


def _bundled_exe() -> Optional[Path]:
    """打包内置 / 开发模式项目内置的 ffmpeg。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", ""))  # type: ignore[attr-defined]
        if base:
            for cand in (base / "ffmpeg.exe", base / "bin" / "ffmpeg.exe"):
                if cand.exists():
                    return cand
        return None
    # 开发模式: 优先用项目自带的 assets/ffmpeg.exe
    dev = Path(__file__).resolve().parent.parent / "assets" / "ffmpeg.exe"
    return dev if dev.exists() else None


def find_ffmpeg() -> Optional[Path]:
    """按优先级查找 ffmpeg.exe, 找不到返回 None。"""
    bundled = _bundled_exe()
    if bundled:
        return bundled
    which = shutil.which("ffmpeg")
    if which:
        return Path(which)
    for d in _COMMON_DIRS:
        exe = d / "ffmpeg.exe"
        if exe.exists():
            return exe
    return None


def ffmpeg_version(ffmpeg: Path) -> str:
    try:
        out = run_hidden([str(ffmpeg), "-version"], capture_output=True, timeout=10)
        text = out.stdout.decode("utf-8", "ignore")
        return text.splitlines()[0] if text else ""
    except Exception:
        return ""
