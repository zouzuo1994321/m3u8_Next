"""探测 QtWebEngine 的编解码器支持能力。

用途: 定位 "This video file cannot be played. (Error Code: 102630)"
是否由内置浏览器缺少 H.264/AAC 解码能力导致。

用法: python tests/probe_codecs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEnginePage

JS = r"""
(function(){
  var v = document.createElement('video');
  function ms(t){ try { return window.MediaSource ? MediaSource.isTypeSupported(t) : null; } catch(e){ return 'err:'+e; } }
  var r = {
    ua: navigator.userAgent,
    has_mse: !!window.MediaSource,
    has_eme: !!navigator.requestMediaKeySystemAccess,
    mse_h264: ms('video/mp4; codecs="avc1.42E01E"'),
    mse_h264_high: ms('video/mp4; codecs="avc1.64001F"'),
    mse_aac: ms('audio/mp4; codecs="mp4a.40.2"'),
    mse_vp9: ms('video/mp4; codecs="vp09.00.10.08"'),
    mse_av1: ms('video/mp4; codecs="av01.0.05M.08"'),
    mse_mp2t: ms('video/mp2t; codecs="avc1.42E01E,mp4a.40.2"'),
    canplay_h264: v.canPlayType('video/mp4; codecs="avc1.42E01E"'),
    canplay_aac: v.canPlayType('audio/mp4; codecs="mp4a.40.2"'),
    canplay_vp8: v.canPlayType('video/webm; codecs="vp8"'),
    canplay_vp9: v.canPlayType('video/webm; codecs="vp9"'),
    canplay_hls: v.canPlayType('application/vnd.apple.mpegurl'),
    canplay_ts: v.canPlayType('video/mp2t')
  };
  return JSON.stringify(r);
})()
"""


def main():
    app = QApplication(sys.argv)
    page = QWebEnginePage()

    def on_result(res):
        print("=== QtWebEngine 能力探测结果 ===")
        print(res)
        print()
        import json
        try:
            d = json.loads(res)
        except Exception:
            app.quit()
            return
        print("H.264 (MSE)      :", d.get("mse_h264"))
        print("H.264 High (MSE) :", d.get("mse_h264_high"))
        print("AAC   (MSE)      :", d.get("mse_aac"))
        print("VP9   (MSE)      :", d.get("mse_vp9"))
        print("AV1   (MSE)      :", d.get("mse_av1"))
        print("TS 容器 (MSE)    :", d.get("mse_mp2t"))
        print("canPlayType H264 :", d.get("canplay_h264"))
        print("canPlayType AAC  :", d.get("canplay_aac"))
        print("canPlayType VP8  :", d.get("canplay_vp8"))
        print("canPlayType VP9  :", d.get("canplay_vp9"))
        print("原生 HLS         :", d.get("canplay_hls"))
        print("MSE 可用         :", d.get("has_mse"))
        print("EME 可用         :", d.get("has_eme"))
        print()
        if not d.get("mse_h264"):
            print(">>> 结论: 缺少 H.264 解码器 —— HLS/MP4 播放会失败 (Error 102630)")
        if not d.get("mse_aac"):
            print(">>> 结论: 缺少 AAC 解码器")
        if d.get("mse_h264") and d.get("mse_aac"):
            print(">>> 结论: H.264/AAC 可用, 102630 不是编解码器缺失导致")
        app.quit()

    def on_load(ok):
        page.runJavaScript(JS, 0, on_result)

    page.loadFinished.connect(on_load)
    page.setHtml("<html><body>probe</body></html>")
    QTimer.singleShot(20000, app.quit)
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
