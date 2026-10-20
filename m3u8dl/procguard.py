"""进程管理: 启动时清理残留进程 + 退出时确保自身进程终止。

- cleanup_stale_instances: exe 启动时杀掉之前未关闭的同名进程(单实例)。
  仅在 PyInstaller frozen 模式下生效, 避免误杀开发环境的 python 进程。
- force_exit: 主循环结束后强制终止自身进程(含所有子线程与 QtWebEngine 子进程)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Optional


def _self_exe() -> Optional[Path]:
    if getattr(sys, "frozen", False):
        try:
            return Path(sys.executable).resolve()
        except Exception:
            return None
    return None


def cleanup_stale_instances(log: Optional[Callable[[str], None]] = None) -> int:
    """清理之前未关闭的同名 exe 进程, 返回清理数量。

    注意: PyInstaller 6.x onefile 为「bootloader 父进程 + 应用子进程」架构,
    两者 exe 路径相同。必须跳过自身(os.getpid)与父进程(os.getppid),
    否则会误杀自己的 bootloader, 导致启动方看到进程立即退出。
    """
    exe = _self_exe()
    if exe is None:
        return 0
    try:
        import psutil
    except ImportError:
        return 0
    killed = 0
    me = os.getpid()
    parent = os.getppid()
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = proc.info
            pexe = info.get("exe")
            if not pexe:
                continue
            pid = info.get("pid")
            if pid in (me, parent):
                continue
            if Path(pexe).resolve() == exe:
                # 先收子进程(QtWebEngineProcess / ffmpeg 等), 再杀主进程
                try:
                    for ch in proc.children(recursive=True):
                        try:
                            ch.kill()
                            killed += 1
                        except Exception:
                            pass
                except Exception:
                    pass
                proc.kill()
                killed += 1
                if log:
                    log(f"已清理残留进程 PID={pid}")
        except Exception:
            continue
    return killed


def terminate_children(log: Optional[Callable[[str], None]] = None) -> int:
    """终止自身派生的所有子进程(QtWebEngineProcess / ffmpeg 等)。

    在 os._exit 之前调用, 避免强制退出留下孤儿子进程。
    """
    try:
        import psutil
    except ImportError:
        return 0
    killed = 0
    try:
        me = psutil.Process()
        for child in me.children(recursive=True):
            try:
                child.kill()
                killed += 1
                if log:
                    log(f"已终止子进程 PID={child.pid} ({child.name()})")
            except Exception:
                pass
    except Exception:
        pass
    return killed


def force_exit(code: int = 0) -> None:
    """强制终止自身进程, 不等待任何后台线程。

    注意: windowed(frozen) 模式下 sys.stdout/sys.stderr 可能为 None 或已关闭,
    flush 前必须判空并容错, 否则 AttributeError 会冒泡出 main(),
    被 PyInstaller bootloader 拦截弹出 "Unhandled exception in script" 对话框,
    导致进程无法退出。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:
            pass
    os._exit(code)
