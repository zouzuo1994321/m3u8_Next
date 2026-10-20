"""诊断日志: 关键启动信息落盘, 便于 frozen(exe) 环境排查。

与 main.py 的 _crash_log 写同一个文件(%TEMP%/m3u8dl_crash.log),
但面向运行期组件状态(媒体后端、注入脚本、代理端口等)。
"""
from __future__ import annotations

import os
import time
from pathlib import Path


def log_file() -> Path:
    return Path(os.environ.get("TEMP", ".")) / "m3u8dl_crash.log"


def log(msg: str) -> None:
    try:
        with open(log_file(), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass
