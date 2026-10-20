"""核心模块单元测试(不依赖网络)。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3u8dl.playlist import (
    format_duration,
    format_size,
    is_master,
    parse_attrs,
    parse_m3u8,
)

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1280000,RESOLUTION=640x360,CODECS="avc1.4d401f,mp4a.40.2"
v360.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2560000,RESOLUTION=1280x720,NAME="720p"
v720.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:42
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x0123456789abcdef0123456789abcdef
#EXTINF:9.9,
seg0.ts
#EXTINF:10.0,
seg1.ts
#EXT-X-ENDLIST
"""

LIVE = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXTINF:4.0,
seg100.ts
#EXTINF:4.0,
seg101.ts
"""

FMP4 = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.0,
frag0.m4s
#EXTINF:4.0,
frag1.m4s
#EXT-X-ENDLIST
"""


class TestPlaylist(unittest.TestCase):
    def test_parse_attrs(self):
        a = parse_attrs('#EXT-X-STREAM-INF:BANDWIDTH=1280000,RESOLUTION=640x360,CODECS="avc1.4d401f,mp4a.40.2"')
        self.assertEqual(a["BANDWIDTH"], "1280000")
        self.assertEqual(a["RESOLUTION"], "640x360")
        self.assertEqual(a["CODECS"], "avc1.4d401f,mp4a.40.2")

    def test_master(self):
        pl = parse_m3u8(MASTER, "https://example.com/hls/master.m3u8")
        self.assertTrue(is_master(MASTER))
        self.assertEqual(len(pl.variants), 2)
        self.assertEqual(pl.variants[0].url, "https://example.com/hls/v360.m3u8")
        self.assertEqual(pl.variants[0].resolution, "640x360")
        self.assertEqual(pl.variants[0].bitrate_kbps, 1280)
        self.assertEqual(pl.variants[1].label, "1280x720 / 2560kbps")

    def test_media(self):
        pl = parse_m3u8(MEDIA, "https://example.com/hls/index.m3u8")
        self.assertFalse(pl.is_live)
        self.assertEqual(len(pl.segments), 2)
        self.assertAlmostEqual(pl.duration, 19.9)
        self.assertEqual(pl.media_sequence, 42)
        self.assertIsNotNone(pl.key)
        self.assertEqual(pl.key.method, "AES-128")
        self.assertEqual(pl.key.uri, "https://example.com/hls/key.bin")
        self.assertEqual(pl.key.iv, "0x0123456789abcdef0123456789abcdef")

    def test_live(self):
        pl = parse_m3u8(LIVE, "https://example.com/live/index.m3u8")
        self.assertTrue(pl.is_live)
        self.assertEqual(len(pl.segments), 2)
        self.assertEqual(pl.segments[0].url, "https://example.com/live/seg100.ts")

    def test_fmp4(self):
        pl = parse_m3u8(FMP4, "https://example.com/hls/index.m3u8")
        self.assertEqual(pl.init_url, "https://example.com/hls/init.mp4")
        self.assertEqual(pl.segments[0].url, "https://example.com/hls/frag0.m4s")

    def test_format(self):
        self.assertEqual(format_duration(3661), "1h01m01s")
        self.assertEqual(format_duration(61), "1m01s")
        self.assertEqual(format_duration(44.5), "44s")
        self.assertEqual(format_duration(0), "")
        self.assertEqual(format_size(1536), "1.50 KB")
        self.assertEqual(format_size(500), "500 B")


class TestSnifferRegex(unittest.TestCase):
    def test_extract_urls(self):
        from m3u8dl.sniffer import Sniffer
        s = Sniffer.__new__(Sniffer)
        html = '''
        <script>
        var a = "https:\\/\\/cdn.example.com\\/path\\/master.m3u8?token=abc";
        var b = 'https://cdn.example.com/video.mp4';
        var c = "https://cdn.example.com/live/index.m3u8";
        </script>
        '''
        urls = s._extract_urls(html)
        self.assertIn("https://cdn.example.com/path/master.m3u8?token=abc", urls)
        self.assertIn("https://cdn.example.com/video.mp4", urls)
        self.assertIn("https://cdn.example.com/live/index.m3u8", urls)
        self.assertEqual(len(urls), 3)

    def test_extract_title(self):
        from m3u8dl.sniffer import Sniffer
        s = Sniffer.__new__(Sniffer)
        self.assertEqual(s._extract_title("<html><title> 测试 视频 - 站点 </title></html>"),
                         "测试 视频 - 站点")


if __name__ == "__main__":
    unittest.main(verbosity=2)
