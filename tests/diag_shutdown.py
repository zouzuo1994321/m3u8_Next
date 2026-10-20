"""针对性诊断: frozen exe 优雅关闭时的异常来源。

流程: 启动 exe -> 等待初始化 -> taskkill(WM_CLOSE, 无/F) -> 等待 ->
      检查残留进程 -> 读取 %TEMP%/m3u8dl_crash.log 输出诊断日志。

用法: python tests/diag_shutdown.py
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

EXE = Path(__file__).resolve().parent.parent / "dist" / "M3U8视频下载器.exe"
CRASH_LOG = Path(os.environ.get("TEMP", ".")) / "m3u8dl_crash.log"


def app_procs():
    result = []
    for p in psutil.process_iter():
        try:
            if "M3U8" in (p.name() or ""):
                result.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def dump(tag):
    ps = app_procs()
    print(f"[{tag}] M3U8 进程 {len(ps)} 个")
    for p in ps:
        try:
            print(f"    PID={p.pid} PPID={p.ppid()} RSS={p.memory_info().rss // 1048576}MB")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return ps


def main():
    if not EXE.exists():
        print(f"exe 不存在: {EXE}")
        sys.exit(1)

    # 清理历史残留 + 旧崩溃日志
    for p in app_procs():
        try:
            p.kill()
        except Exception:
            pass
    time.sleep(1)
    if CRASH_LOG.exists():
        CRASH_LOG.unlink()
    print(f"崩溃日志路径: {CRASH_LOG} (已清空)")

    # 启动
    p1 = subprocess.Popen([str(EXE)])
    print(f"已启动 exe, bootloader PID={p1.pid}, 等待 20 秒初始化...")
    time.sleep(20)
    ps = dump("启动20秒后")
    children = [p for p in ps if p.ppid() == p1.pid]
    if not children:
        print("!! 未找到应用子进程, 异常, 强制退出")
        for p in ps:
            p.kill()
        sys.exit(1)
    child = children[0]
    print(f"应用子进程 PID={child.pid}")

    # 优雅关闭: taskkill 无 /F = 发 WM_CLOSE
    print("发送 WM_CLOSE (taskkill 无 /F)...")
    r = subprocess.run(["taskkill", "/PID", str(child.pid)],
                       capture_output=True)
    print(f"taskkill 输出: {r.stdout.decode('gbk', 'replace').strip()}")

    # 等待退出
    for i in range(12):
        time.sleep(1)
        ps = dump(f"WM_CLOSE 后 {i + 1}s")
        if not ps:
            print(f"\n>>> {i + 1} 秒内全部退出, 优雅关闭成功!")
            break
    else:
        print("\n>>> 12 秒后仍有残留进程!")

    # 读取崩溃日志
    print("\n" + "=" * 60)
    if CRASH_LOG.exists():
        print(f"崩溃日志内容 ({CRASH_LOG}):")
        print("-" * 60)
        print(CRASH_LOG.read_text(encoding="utf-8", errors="replace"))
        print("-" * 60)
    else:
        print("崩溃日志不存在 (未触发 excepthook)")

    # 清理残留
    for p in app_procs():
        try:
            p.kill()
        except Exception:
            pass
    time.sleep(1)
    ps = dump("最终清理后")
    print("\n诊断结束。" + (" 全部通过!" if not ps else " 仍有残留(见上)"))


if __name__ == "__main__":
    main()
