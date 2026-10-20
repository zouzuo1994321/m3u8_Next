"""版本信息管理。

版本号规则:
- 内部版本号: YYMMDDNNNN(年月日 + 4位当日迭代序号), 沿用 2609010026 的规则。
  例: 2610200001 = 2026-10-20 当日第 1 次构建。
- 显示版本:   vMAJOR.MINOR.PATCH
  - 第二位 MINOR: 大版本迭代时 +1
  - 第三位 PATCH: 小版本迭代时 +1
"""

APP_NAME = "M3U8视频下载器"
APP_NAME_EN = "M3U8 Video Downloader"
APP_ID = "m3u8-video-downloader"

VERSION = "v1.1.21"
BUILD = "2610200023"
BUILD_DATE = "2026-10-20"

AUTHOR = "肆月Aperture"
COPYRIGHT = "Copyright © 2026 肆月Aperture"
LICENSE_NOTE = "本软件为开源软件，未经授权禁止用于商业用途。"

VERSION_RULES = (
    "内部版本号: YYMMDDNNNN(年月日+4位迭代序号)\n"
    "显示版本: v主版本.大版本迭代.小版本迭代"
)
