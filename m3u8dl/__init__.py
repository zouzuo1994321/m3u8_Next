"""M3U8视频下载器 —— 基于 REA 逆向分析思路, 原生复刻猫抓(cat-catch)与
N_m3u8DL-CLI 两个开源项目核心能力的 PySide6 桌面端实现。

模块划分:
- playlist   : m3u8 播放列表解析(HLS RFC 8216)
- preview    : 内置实时预览(QMediaPlayer/FFmpeg + 本地代理)
- downloader : m3u8 下载引擎(复刻 N_m3u8DL-CLI)
- sniffer    : 网页视频嗅探(复刻猫抓)
- websniff   : 浏览器引擎嗅探(网络请求拦截)
- webinject  : 页面脚本注入(编解码器能力伪装 + 地址钩子)
- cookies    : 浏览器 Cookie 罐(防盗链登录态回放)
- fftools    : ffmpeg 探测与合并
- procguard  : 进程管理(启动清理/退出终止)
- ui         : PySide6 深色界面
"""
from . import version

__version__ = version.VERSION
__build__ = version.BUILD
__app_name__ = version.APP_NAME
