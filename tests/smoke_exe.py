"""exe 冒烟测试: 启动存活 / 单实例清理 / 优雅关闭无残留。

用法: python tests/smoke_exe.py
"""
import subprocess
import sys
import time
from pathlib import Path

import psutil

EXE = Path(__file__).resolve().parent.parent / "dist" / "M3U8视频下载器.exe"


def app_procs():
    result = []
    for p in psutil.process_iter():
        try:
            if "M3U8" in (p.name() or ""):
                result.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def webengine_procs():
    result = []
    for p in psutil.process_iter():
        try:
            if (p.name() or "") == "QtWebEngineProcess.exe":
                result.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def dump(tag):
    ps = app_procs()
    print(f"[{tag}] M3U8进程 {len(ps)} 个, QtWebEngineProcess {len(webengine_procs())} 个")
    for p in ps:
        try:
            pid = p.pid
            ppid = p.ppid()
            rss = p.memory_info().rss // 1024 // 1024
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        print(f"    PID={pid} PPID={ppid} RSS={rss}MB")
    return ps


def main():
    if not EXE.exists():
        print(f"exe 不存在: {EXE}")
        sys.exit(1)

    # 清理可能的历史残留
    for p in app_procs() + webengine_procs():
        try:
            p.kill()
        except Exception:
            pass
    time.sleep(1)

    # ---- 1. 启动实例1, 验证 bootloader 与应用子进程共存 ----
    p1 = subprocess.Popen([str(EXE)])
    time.sleep(25)
    ps = dump("实例1启动25秒")
    assert p1.poll() is None, "bootloader 应存活(修复误杀父进程后)"
    assert len(ps) >= 2, "应有 bootloader + 应用子进程两个进程"
    assert len(webengine_procs()) >= 1, "QtWebEngineProcess 应已启动(证明 Qt 初始化成功)"

    # ---- 2. 单实例: 启动实例2, 实例1 应被整体清理 ----
    p2 = subprocess.Popen([str(EXE)])
    time.sleep(18)
    ps = dump("实例2启动后")
    assert p1.poll() is not None, "实例1 的 bootloader 应被清理"
    assert len(ps) == 2, f"应只剩实例2 的两个进程, 实际 {len(ps)}"
    assert all(p.ppid() == p2.pid or p.pid == p2.pid for p in ps), \
        "残留进程应全部属于实例2"

    # ---- 3. 优雅关闭: 对应用子进程发 WM_CLOSE(等价于点右上角 X) ----
    children = [p for p in ps if p.ppid() == p2.pid]
    assert children, "应找到实例2 的应用子进程"
    child = children[0]
    print(f"对应用子进程 PID={child.pid} 发送 WM_CLOSE(优雅关闭)")
    subprocess.run(["taskkill", "/PID", str(child.pid)], capture_output=True)
    # 轮询等待退出(onefile 需先由 bootloader 回收解压目录, 机器繁忙时可能超过 10s)
    deadline = time.time() + 40
    while time.time() < deadline:
        if not app_procs() and not webengine_procs():
            break
        time.sleep(1)
    ps = dump("优雅关闭后")
    assert len(ps) == 0, f"关闭后不应有 M3U8 残留进程, 实际 {len(ps)}"
    assert len(webengine_procs()) == 0, "关闭后不应有 QtWebEngineProcess 残留"

    print("\nEXE SMOKE OK: 启动存活 / 单实例清理 / 优雅关闭无残留 全部通过")


if __name__ == "__main__":
    main()
