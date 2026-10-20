"""验证 QMediaPlayer(FFmpeg 后端) 能否播放 H.264 + AAC 的 HLS 流。

背景: QtWebEngine 缺 H.264/AAC 解码器(已实测), 页面内播放必然失败;
若 QMediaPlayer(FFmpeg) 可播, 则可作为真正的预览方案。

用法: python tests/probe_mediaplayer.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
FFMPEG = ROOT / "assets" / "ffmpeg.exe"


def make_hls(dirpath: Path) -> Path:
    """用 ffmpeg 生成 6 秒 H.264+AAC 的 HLS 测试流。"""
    m3u8 = dirpath / "test.m3u8"
    cmd = [
        str(FFMPEG), "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
        "-hls_segment_filename", str(dirpath / "seg%03d.ts"),
        str(m3u8),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return m3u8


def main():
    os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink

    app = QApplication(sys.argv)

    tmp = Path(tempfile.mkdtemp(prefix="m3u8dl_mptest_"))
    m3u8 = make_hls(tmp)
    print(f"测试流: {m3u8}")
    print(f"分片数: {len(list(tmp.glob('*.ts')))}")

    player = QMediaPlayer()
    sink = QVideoSink()
    player.setVideoSink(sink)

    state = {"frames": 0, "err": "", "status": None}

    def on_frame(f):
        if not f.isValid():
            return
        state["frames"] += 1

    sink.videoFrameChanged.connect(on_frame)
    player.errorOccurred.connect(
        lambda e, s: state.__setitem__("err", f"{e}: {s}"))
    player.mediaStatusChanged.connect(
        lambda st: state.__setitem__("status", st))
    player.playbackStateChanged.connect(
        lambda st: print("  playbackState:", st))

    print("后端候选:", os.environ.get("QT_MEDIA_BACKEND"))
    player.setSource(QUrl.fromLocalFile(str(m3u8)))
    player.play()

    def finish():
        print()
        print("=== 结果 ===")
        print("解码出的视频帧数:", state["frames"])
        print("媒体状态:", state["status"])
        print("错误:", state["err"] or "(无)")
        if state["frames"] > 0:
            print(">>> QMediaPlayer 可播放 H.264 HLS —— 可作为预览方案")
        else:
            print(">>> QMediaPlayer 未能解码, 需换方案")
        player.stop()
        app.quit()

    QTimer.singleShot(8000, finish)
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
