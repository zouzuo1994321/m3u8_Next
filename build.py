"""PyInstaller onefile 打包脚本(带体积裁剪)。

用法: python build.py
产物: dist/M3U8视频下载器.exe(内置 ffmpeg.exe)

v1.1.21 起改成 **spec 方式**打包 —— 命令行参数只能在"收哪些"上做加法, 收不回来;
而 onefile 包里真正的大头是**用不到的 Qt 资源**, 只能靠 Analysis 之后过滤 TOC:

| 内容 | 原体积 | 处理 |
| --- | --- | --- |
| `PySide6/resources/*.debug.pak` `*.debug.bin` | 78 MB | 删(DevTools 符号资源, 运行期不用) |
| `PySide6/translations/*.qm`(305 个语种) | 57 MB | 只留 `*_zh_CN.qm` 13 个 |
| `PySide6/qml/**`(2515 个文件) | 24 MB | 删(QtWebEngineWidgets 走 C++ 侧, 不用 QML) |
| 用不到的 Qt 模块(Qt3D/Pdf/Designer/Graphs/Charts…) | ~45 MB | 不再 `--collect-submodules PySide6` |
| 用不到的插件(sqldrivers/designer/canbus/qmltooling…) | ~10 MB | 按白名单过滤 |

**关键: 不再 `--collect-submodules PySide6`**。那一行会把 PySide6 里全部子模块
(含 Qt3D/QtCharts/QtPdf/QtDesigner…)连同 DLL、QML、翻译一起收进来 —— 项目实际
只用 8 个模块(QtCore/Gui/Widgets/WebEngineCore/WebEngineWidgets/Multimedia/
MultimediaWidgets/WebSockets/WebChannel), 其余全是死重。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = Path(r"C:\Users\zouzu\.workbuddy\binaries\python\envs\default\Scripts\python.exe")
NAME = "M3U8视频下载器"

#: 只留这些插件目录(platforms/styles 是基础; imageformats 管图标解码;
#: multimedia 是预览的 FFmpeg 后端; tls 给 WebEngine 的 TLS; 其余按需)
KEEP_PLUGINS = {
    "platforms", "styles", "imageformats", "multimedia", "tls",
    "networkinformation", "iconengines", "platforminputcontexts", "generic",
}

#: PySide6 里明确用不到的子模块(不作为 hidden import, 也不让钩子收进来)
EXCLUDES = [
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtQuick3D", "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtUiTools", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtScxml",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtStateMachine",
    "PySide6.QtRemoteObjects", "PySide6.QtSvgWidgets", "PySide6.QtOpenGLWidgets",
    # Python 侧无用大件
    "tkinter", "unittest", "pytest", "setuptools", "pip", "wheel",
]

SPEC = f"""# -*- mode: python ; coding: utf-8 -*-
# 本文件由 build.py 生成, 请改 build.py
import os

ROOT = {str(ROOT)!r}
ASSETS = os.path.join(ROOT, "assets")

datas = [
    (os.path.join(ASSETS, "hls.min.js"), "assets"),
    (os.path.join(ASSETS, "icon.ico"), "assets"),
    (os.path.join(ASSETS, "icon.png"), "assets"),
]
_ff = os.path.join(ASSETS, "ffmpeg.exe")
if os.path.exists(_ff):
    datas.append((_ff, "."))

binaries = []
try:
    import PySide6 as _ps
    _site = os.path.dirname(os.path.abspath(_ps.__file__))
    # QtMultimedia 的 FFmpeg 后端插件 + 解码库(预览用)
    _plug = os.path.join(_site, "plugins", "multimedia")
    if os.path.isdir(_plug):
        for _f in os.listdir(_plug):
            _p = os.path.join(_plug, _f)
            if os.path.isfile(_p):
                binaries.append((_p, os.path.join("PySide6", "plugins", "multimedia")))
    for _f in os.listdir(_site):
        if _f.startswith(("avcodec-", "avformat-", "avutil-", "swresample-",
                          "swscale-", "avfilter-", "avdevice-")) and _f.endswith(".dll"):
            binaries.append((os.path.join(_site, _f), "PySide6"))
except Exception as _e:
    print("WARN: 收集 QtMultimedia 资源失败:", _e)

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=["PySide6.QtWebSockets"],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes={EXCLUDES!r},
    noarchive=False,
    optimize=0,
)

# ---------------------------------------------------------------- 体积裁剪
KEEP_PLUGINS = {sorted(KEEP_PLUGINS)!r}


def _keep(dest):
    d = dest.replace("\\\\", "/").lower()
    # 1. DevTools / 调试符号资源(78 MB): 运行期只读非 debug 版
    if d.endswith(".debug.pak") or d.endswith(".debug.bin"):
        return False
    # 2. 翻译只留简体中文(305 个语种共 57 MB)
    if "/translations/" in d and "_zh_cn" not in d:
        return False
    # 3. QML 模块目录(24 MB): 本项目不用 QML, WebEngine 走 Widgets/C++ 侧
    if "/qml/" in d:
        return False
    # 4. Qt 插件白名单
    if "/plugins/" in d:
        parts = d.split("/plugins/", 1)[1].split("/")
        if parts and parts[0] not in KEEP_PLUGINS:
            return False
    return True


_before = len(a.datas) + len(a.binaries)
a.datas = [x for x in a.datas if _keep(x[0])]
a.binaries = [x for x in a.binaries if _keep(x[0])]
print(f"[体积裁剪] TOC 条目 {{_before}} -> {{len(a.datas) + len(a.binaries)}}")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name={NAME!r},
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(ASSETS, "icon.ico"),
)
"""


def main():
    spec = ROOT / f"{NAME}.spec"
    spec.write_text(SPEC, encoding="utf-8")

    cmd = [
        str(VENV_PY), "-m", "PyInstaller", "--noconfirm",
        # 注意: 传 .spec 时不能再带 --onefile / --noconsole / --icon 之类
        # 的 makespec 参数(PyInstaller 会直接报 option not allowed) —— 这些
        # 都已经写在 spec 里了。
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        str(spec),
    ]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))

    exe = ROOT / "dist" / f"{NAME}.exe"
    mb = exe.stat().st_size / 1048576 if exe.exists() else 0
    print(f"\n构建完成: {exe}  ({mb:.1f} MB)")


if __name__ == "__main__":
    sys.exit(main())
