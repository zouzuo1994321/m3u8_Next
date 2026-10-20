"""更多功能工具集测试: 图片伪装剥离 / 分片参数 / 正则筛选 / 密钥爆破。

用法: python tests/test_fragtools.py

对应猫抓 ``js/m3u8.js`` 的：
- ``#dataPreprocessing`` 数据预处理（剥 PNG/JPG 伪装头）
- ``#tsAddArg`` 分片参数追加
- ``#regular`` 正则筛选 + ``#invertSelection`` 反选
- ``#downText`` 导出分片列表
- ``#searchingForRealKey`` 用容器签名爆破真 KEY
"""
import base64
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from m3u8dl import fragtools as ft
from m3u8dl.playlist import Segment


# ---------------------------------------------------------------- 构造样本
PNG_HEAD = (b"\x89PNG\r\n\x1a\n"          # signature
            + b"\x00\x00\x00\x0dIHDR" + b"\x00" * 13 + b"\xaeB`\x82"
            + b"IEND" + b"\xaeB`\x82")   # IEND + CRC
JPG_HEAD = b"\xff\xd8\xff\xe0" + b"\x00" * 12 + b"\xff\xd9"

TS_BODY = (b"\x47" + b"\x00" * 187) * 4      # 4 个 TS 包
MP4_BODY = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24


def encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    pad = 16 - (len(plain) % 16)
    data = plain + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def main():
    # ---- 1. 图片伪装剥离 ----
    assert ft.detect_wrapper(PNG_HEAD + TS_BODY) == "png"
    assert ft.strip_image_wrapper(PNG_HEAD + TS_BODY) == TS_BODY
    assert ft.detect_wrapper(JPG_HEAD + MP4_BODY) == "jpeg"
    assert ft.strip_image_wrapper(JPG_HEAD + MP4_BODY) == MP4_BODY
    print("[PASS] PNG/JPG 伪装头剥离, 只保留后面的媒体载荷")

    # 无伪装的数据必须原样返回（宁可不处理也不能破坏）
    raw = TS_BODY
    assert ft.strip_image_wrapper(raw) == raw
    # 有头但缺结束标记 -> 原样返回
    broken = PNG_HEAD[:8] + b"\x00" * 20
    assert ft.strip_image_wrapper(broken) == broken
    assert ft.strip_image_wrapper(b"") == b""
    assert ft.strip_image_wrapper(b"abc") == b"abc"
    print("[PASS] 无伪装/残损数据原样返回(不破坏字节)")

    # ---- 2. 分片参数追加 ----
    u = "https://cdn.example.com/a/seg1.ts?sign=old&t=1"
    out = ft.append_query_params(u, "sign=new&exp=999")
    assert "sign=new" in out and "exp=999" in out, out
    assert out.count("sign=") == 1, f"同名参数应覆盖而非重复: {out}"
    assert "t=1" in out, "原有参数应保留"

    u2 = "https://cdn.example.com/a/seg1.ts"
    out2 = ft.append_query_params(u2, "?token=abc")
    assert out2 == u2 + "?token=abc", out2
    assert ft.append_query_params(u2, "") == u2
    print("[PASS] 分片参数追加: 同名覆盖/保留原有/空参数不改变地址")

    # ---- 3. 正则筛选与反选 ----
    urls = [
        "https://x/seg_1.ts",
        "https://x/seg_2.ts",
        "https://x/ads/trailer_1.ts",
        "https://x/seg_3.ts",
    ]
    flags = ft.select_by_regex(urls, r"seg_\d+\.ts$")
    assert flags == [True, True, False, True], flags
    inv = ft.invert_selection(flags)
    assert inv == [False, False, True, False], inv
    assert ft.selected_indices(flags) == [0, 1, 3]
    assert ft.select_by_regex(urls, "") == [True] * 4
    try:
        ft.select_by_regex(urls, "([")
        raise AssertionError("无效正则应抛错")
    except ValueError as e:
        assert "正则" in str(e)
    print("[PASS] 正则筛选 / 反选 / 空规则全选 / 无效正则报错")

    # ---- 4. 导出分片列表 ----
    segs = [Segment(index=i, url=f"https://x/s{i}.ts", duration=6.0)
            for i in range(5)]
    tmp = Path(tempfile.mkdtemp(prefix="m3u8dl_frag_"))
    p, n = ft.export_fragment_list(segs, tmp / "list.txt")
    assert n == 5 and p.exists()
    lines = p.read_text(encoding="utf-8").strip().split("\n")
    assert lines == [s.url for s in segs], lines
    p2, n2 = ft.export_fragment_list(segs, tmp / "sub.txt", indices=[0, 2, 4])
    assert n2 == 3
    assert p2.read_text(encoding="utf-8").strip().split("\n") == \
        [segs[0].url, segs[2].url, segs[4].url]
    print("[PASS] 导出分片列表: 全量 / 仅勾选, 行序与分片一致")

    # ---- 5. 容器签名识别 ----
    assert ft.looks_like_decrypted(MP4_BODY)
    assert ft.looks_like_decrypted(TS_BODY)
    assert ft.looks_like_decrypted(b"\x1a\x45\xdf\xa3" + b"\x00" * 20)   # webm
    assert ft.looks_like_decrypted(b"ID3" + b"\x00" * 20)                 # mp3
    assert not ft.looks_like_decrypted(b"\x00" * 64)
    assert not ft.looks_like_decrypted(b"")
    print("[PASS] 容器签名识别: fMP4 / TS / WebM / ID3 命中, 乱码不命中")

    # ---- 6. 密钥爆破 ----
    key = bytes(range(16))
    wrong1, wrong2 = bytes(16), bytes([0xFF] * 16)
    iv = (7).to_bytes(16, "big")
    cipher_text = encrypt(TS_BODY, key, iv)

    found = ft.brute_force_key(cipher_text, [wrong1, wrong2, key], iv)
    assert found == key, f"应找出正确 KEY, 实际 {found}"
    print("[PASS] 密钥爆破: 从 3 个候选里挑出能解出合法容器的那个")

    # 伪装过的密文: 解密后是 PNG+TS, 仍应识别
    wrapped = encrypt(PNG_HEAD + TS_BODY, key, iv)
    assert ft.brute_force_key(wrapped, [wrong1, key], iv) == key
    print("[PASS] 解密结果带图片伪装时也能验证通过(先剥离再判签名)")

    assert ft.brute_force_key(cipher_text, [wrong1, wrong2], iv) is None
    assert ft.brute_force_key(b"", [key], iv) is None
    assert ft.brute_force_key(cipher_text, [], iv) is None
    print("[PASS] 候选全错/空数据/空候选 均返回 None")

    try:
        ft.brute_force_key(cipher_text, [key], b"short")
        raise AssertionError("IV 长度错误应抛异常")
    except ValueError:
        pass
    print("[PASS] IV 长度校验")

    # ---- 7. 密钥编码 ----
    assert ft.key_to_hex(key) == "000102030405060708090a0b0c0d0e0f"
    assert base64.b64decode(ft.key_to_b64(key)) == key
    print("[PASS] KEY 十六进制 / base64 编码输出")

    # ---- 8. 仅音频参数 ----
    assert "-vn" in ft.audio_only_args()
    assert ft.audio_only_args(["-vn"]).count("-vn") == 1
    print("[PASS] 仅音频参数去重")

    # ---- 9. 伪装提示文案 ----
    msg = ft.describe_wrapper(PNG_HEAD + TS_BODY)
    assert "PNG" in msg and "剥离" in msg, msg
    assert ft.describe_wrapper(TS_BODY) == ""
    print(f"[PASS] 伪装提示: {msg}")

    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    print("\nFRAGTOOLS TEST OK: 更多功能工具集全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
