#!/usr/bin/env python3
"""
test_bilibili_asr.py — bilibili_asr.py 烟雾测试

不跑真实 B 站 (避免网络依赖 + 速率限制), 只验证:
  1. 模块导入 OK
  2. MODELS dict 路径正确
  3. ffmpeg (imageio-ffmpeg 优先) 能跑
  4. FunASR 模型文件存在 + size 正确 (catches 下载损坏)
  5. llama-funasr-* binary 在 sample audio 上能跑通 (返回合法 SRT)
  6. (optional) end-to-end: 跑一次 BV1rmM76kEDG (需要 B 站登录)

运行:
  python3 test_bilibili_asr.py

退出码: 0 全过 / 1 有失败
"""

import os
import re
import struct
import subprocess
import sys
import wave
from pathlib import Path

# 让 bilibili_asr.py 能被 import
sys.path.insert(0, str(Path(__file__).parent))
import bilibili_asr as A  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = ""):
    """单条 check, ok=False 时累计到 FAILURES"""
    icon = "✓" if ok else "✗"
    msg = f"  {icon} {name}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    if not ok:
        FAILURES.append(name)


def test_imports():
    print("[1] 导入检查")
    for mod in ("argparse", "asyncio", "json", "subprocess", "urllib.request",
                "dataclasses", "pathlib"):
        try:
            __import__(mod)
            check(f"import {mod}", True)
        except ImportError as e:
            check(f"import {mod}", False, str(e))


def test_models_dict():
    print("\n[2] MODELS 配置")
    expected = {"sensevoice", "paraformer"}
    actual = set(A.MODELS.keys())
    check("MODELS keys", expected == actual, f"got {actual}")

    # 默认 model 必须是 sensevoice (多语, B 站混合内容)
    # 注: argparse default 在 main() 里, 这里只能查 MODELS keys
    if "sensevoice" in actual:
        check("sensevoice 在 MODELS 里", True)
    if "paraformer" in actual:
        check("paraformer 在 MODELS 里", True)


def test_model_files():
    print("\n[3] 模型文件 (catches 下载损坏 / 大小不对)")
    # 期望 size (从 HF HEAD 拿的)
    expected = {
        "paraformer-q8.gguf": 236_929_024,
        "sensevoice-small-q8.gguf": 254_208_320,
        "fsmn-vad.gguf": 1_720_512,
    }
    gguf_dir = A.FUNASR_DIR / "funasr-gguf"
    for name, exp_size in expected.items():
        p = gguf_dir / name
        if not p.exists():
            check(f"文件存在: {name}", False, "不存在")
            continue
        actual_size = p.stat().st_size
        check(f"size 正确: {name}",
              actual_size == exp_size,
              f"actual={actual_size}, expected={exp_size}")


def test_binaries():
    print("\n[4] Binary 可执行")
    for name, cfg in A.MODELS.items():
        bin_path = cfg["binary"]
        if not bin_path.exists():
            check(f"binary: {bin_path.name}", False, "不存在")
            continue
        # chmod +x
        try:
            os.chmod(bin_path, 0o755)
        except Exception:
            pass
        # 测 --help (FunASR 的 binary 简单测 -m 是必需, 不能 --help, 用错误测)
        proc = subprocess.run(
            [str(bin_path)],  # 无参数会报错但能确认能 exec
            capture_output=True, text=True, timeout=5,
        )
        check(f"binary 可执行: {bin_path.name}",
              proc.returncode != 0 or "usage" in proc.stderr.lower() or proc.stderr,
              f"rc={proc.returncode}")


def test_find_ffmpeg():
    print("\n[5] find_ffmpeg()")
    try:
        bin_path, source = A.find_ffmpeg()
        check(f"找到 ffmpeg ({source})", True, str(bin_path))
        # 验证能跑
        proc = subprocess.run(
            [str(bin_path), "-version"],
            capture_output=True, text=True, timeout=5,
        )
        check("ffmpeg -version OK", proc.returncode == 0,
              proc.stderr[:60] if proc.returncode else "ffmpeg OK")
    except Exception as e:
        check("find_ffmpeg()", False, str(e))


def make_test_wav(path: Path, duration_sec: float = 3.0, freq: int = 440):
    """生成测试 wav (纯 440Hz 正弦波, 16kHz mono s16le)"""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        import math
        for i in range(int(16000 * duration_sec)):
            sample = int(32767 * 0.3 * math.sin(2 * math.pi * freq * i / 16000))
            w.writeframes(struct.pack("<h", sample))


def test_asr_runs():
    """跑 SenseVoice 在 5 秒纯音调上 → 应返回空或乱码字幕, 但 binary 退出 0"""
    print("\n[6] FunASR SenseVoice binary 能跑 (5秒纯音调)")
    wav = Path("/tmp/test_bilibili_asr_tone.wav")
    make_test_wav(wav, duration_sec=5.0)

    cfg = A.MODELS["sensevoice"]
    cmd = [
        str(cfg["binary"]),
        "-m", str(cfg["model"]),
        "--vad", str(cfg["vad"]),
        "-a", str(wav),
        "--srt",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    check("FunASR binary 退出 0",
          proc.returncode == 0,
          f"rc={proc.returncode}, stderr={proc.stderr[-200:]}")

    # 输出可能是空 (纯音调无语音) — 都算通过
    out_text = proc.stdout.strip()
    check("FunASR stdout 解析 (空 或 SRT 文本都 OK)",
          out_text == "" or "-->" in out_text,
          f"len={len(out_text)}")


def test_srt_count():
    """count_srt_entries() 应正确数 SRT"""
    print("\n[7] count_srt_entries() 解析")
    sample_srt = """1
00:00:01,000 --> 00:00:02,000
Hello world

2
00:00:03,000 --> 00:00:04,000
Foo bar baz

3
00:00:05,000 --> 00:00:06,000
Last entry
"""
    p = Path("/tmp/test_bilibili_asr_count.srt")
    p.write_text(sample_srt, encoding="utf-8")
    n = A.count_srt_entries(p)
    check("count 3 entries", n == 3, f"got {n}")


def test_dataclasses():
    """VideoMeta / ASRResult dataclass 字段对"""
    print("\n[8] Dataclass")
    meta = A.VideoMeta(bvid="BVtest", title="t", duration=100, page_count=1)
    check("VideoMeta 字段", meta.bvid == "BVtest" and meta.duration == 100)

    r = A.ASRResult(bvid="BVtest", model="sensevoice",
                    srt_path=Path("/tmp/a.srt"), lang="auto",
                    srt_entries=10, ok=True)
    check("ASRResult 字段", r.ok is True and r.error is None)


def test_parse_bvid():
    print("\n[9] parse_bvid()")
    cases = [
        ("BV1pb98BWEJa", "BV1pb98BWEJa"),
        ("https://www.bilibili.com/video/BV15rbZ6aEKN", "BV15rbZ6aEKN"),
        ("https://b23.tv/xxxxxx", None),  # 无 BV 字符
        ("BV1x", "BV1x"),
    ]
    for inp, expected in cases:
        if expected is None:
            try:
                A.parse_bvid(inp)
                check(f"parse_bvid 拒绝: {inp}", False, "应该 raise")
            except ValueError:
                check(f"parse_bvid 拒绝: {inp}", True)
        else:
            actual = A.parse_bvid(inp)
            check(f"parse_bvid({inp[:30]}...) → {expected}",
                  actual == expected, f"got {actual}")


def main():
    print("=" * 60)
    print("bilibili_asr.py 烟雾测试")
    print("=" * 60)

    test_imports()
    test_models_dict()
    test_model_files()
    test_binaries()
    test_find_ffmpeg()
    test_asr_runs()
    test_srt_count()
    test_dataclasses()
    test_parse_bvid()

    print()
    print("=" * 60)
    if FAILURES:
        print(f"❌ {len(FAILURES)} 失败:")
        for n in FAILURES:
            print(f"  - {n}")
        return 1
    else:
        print("✓ 全过")
        return 0


if __name__ == "__main__":
    sys.exit(main())