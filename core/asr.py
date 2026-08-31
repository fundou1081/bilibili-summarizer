#!/usr/bin/env python3
"""
bilibili_asr.py — 给 B 站无字幕视频自动生成字幕 (FunASR Paraformer 本地 ASR)

工作流:
  BVID/URL → yt-dlp 下载音频 (m4a) → ffmpeg 转 wav (16kHz mono)
          → llama-funasr-paraformer (FunASR, CPU, --srt --vad)
          → 自动 SRT → downloads/{bvid}/auto-zh.srt

设计原则:
  - 不动 summarize.py / fetch.py / bilibili_cc.py (独立 wrapper)
  - 严格模式: 失败 → 显式报错, 不静默 fallback
  - 结构化: BVID/title/duration/page/meta 用 dataclass / dict
  - 复用 downloads/{bvid}/ 目录结构 (跟 bilibili_cc.py 一致)

用法:
  # 单视频
  python3 bilibili_asr.py <BVID>
  python3 bilibili_asr.py <URL>

  # 批量 (每行一个 BVID/URL)
  python3 bilibili_asr.py --batch bvids.txt

  # 增量 (跳过已有 auto-*.srt)
  python3 bilibili_asr.py --batch bvids.txt --incremental

  # 指定模型 (默认 paraformer)
  python3 bilibili_asr.py <BVID> --model sensevoice

  # 强制重新生成 (覆盖已有 auto-*.srt)
  python3 bilibili_asr.py <BVID> --force

依赖:
  - yt-dlp (已装)
  - ffmpeg (已装)
  - ~/my_proj/bilibili-summarizer/funasr_runtime/
      ├── llama-funasr-paraformer (binary)
      └── funasr-gguf/{paraformer-q8.gguf, fsmn-vad.gguf}

退出码:
  0  全部成功
  1  参数错误
  2  部分视频失败 (至少一个成功)
  3  全部失败
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

PROJECT_DIR = Path(__file__).parent.parent.resolve()
FUNASR_DIR = PROJECT_DIR.parent / "funasr_runtime"
DOWNLOADS_DIR = PROJECT_DIR / "downloads"
CRED_FILE = PROJECT_DIR / ".credential.json"

# ── 模型 binary 与默认 GGUF 路径 ─────────────────────────────────────
MODELS = {
    # 默认 sensevoice: 多语 (zh/en/ja/ko/yue), 自动检测, CPU 实时
    "sensevoice": {
        "binary": FUNASR_DIR / "llama-funasr-sensevoice",
        "model": FUNASR_DIR / "funasr-gguf" / "sensevoice-small-q8.gguf",
        "vad": FUNASR_DIR / "funasr-gguf" / "fsmn-vad.gguf",
        "lang_default": "auto",  # 多语, 自动识别
    },
    # 可选 paraformer: 中文专用 (AISHELL CER 1.95%), 英文内容效果差
    "paraformer": {
        "binary": FUNASR_DIR / "llama-funasr-paraformer",
        "model": FUNASR_DIR / "funasr-gguf" / "paraformer-q8.gguf",
        "vad": FUNASR_DIR / "funasr-gguf" / "fsmn-vad.gguf",
        "lang_default": "zh",
    },
}

# ── 数据结构 ────────────────────────────────────────────────────────
@dataclass
class VideoMeta:
    bvid: str
    title: str = ""
    duration: int = 0
    page_count: int = 1
    uploader: str = ""
    pubdate: int = 0

    @classmethod
    def from_bili_info(cls, bvid: str, info: dict) -> "VideoMeta":
        return cls(
            bvid=bvid,
            title=info.get("title", ""),
            duration=info.get("duration", 0),
            page_count=len(info.get("pages", [])),
            uploader=info.get("owner", {}).get("name", ""),
            pubdate=info.get("pubdate", 0),
        )


@dataclass
class ASRResult:
    bvid: str
    model: str
    srt_path: Path
    lang: str
    duration_sec: float = 0.0
    audio_bytes: int = 0
    wav_bytes: int = 0
    srt_entries: int = 0
    ok: bool = True
    error: Optional[str] = None


# ── 工具函数 ────────────────────────────────────────────────────────
BVID_RE = re.compile(r'BV[A-Za-z0-9]+')


def parse_bvid(url_or_bvid: str) -> str:
    """从 URL / 短链 / 裸 BVID 中抽出 BVID。找不到报错 (禁止 silent fallback)。"""
    m = BVID_RE.search(url_or_bvid)
    if not m:
        raise ValueError(f"无法从输入中提取 BVID: {url_or_bvid!r}")
    return m.group(0)


def resolve_b23(url: str) -> str:
    """解析 b23.tv 短链"""
    if "b23.tv" not in url:
        return url
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.url
    except Exception as e:
        print(f"  [WARN] 解析短链失败 ({e}), 用原始 URL")
        return url


def load_credential() -> Optional[dict]:
    if not CRED_FILE.exists():
        return None
    try:
        return json.loads(CRED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


async def fetch_video_meta(bvid: str) -> VideoMeta:
    """从 B 站 API 拿视频元数据 (title, duration, ...)"""
    try:
        from bilibili_api import video, Credential
    except ImportError:
        raise RuntimeError("bilibili_api 未安装, 运行: pip install bilibili-api-python")

    cred_data = load_credential()
    cred = None
    if cred_data:
        cred = Credential(
            sessdata=cred_data.get("sessdata"),
            bili_jct=cred_data.get("bili_jct"),
            dedeuserid=cred_data.get("dedeuserid"),
        )

    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    return VideoMeta.from_bili_info(bvid, info)


async def get_audio_url(bvid: str, cid: int) -> str:
    """用 bilibili_api 拿 DASH 音频直链 (m4s)。带签名 token, 服务器已授权。
    Returns: 带 query string 的 https URL (临时有效, 一般几小时内可用)。"""
    try:
        from bilibili_api import video, Credential
    except ImportError:
        raise RuntimeError("bilibili_api 未安装")

    cred_data = load_credential()
    cred = None
    if cred_data:
        cred = Credential(
            sessdata=cred_data.get("sessdata"),
            bili_jct=cred_data.get("bili_jct"),
            dedeuserid=cred_data.get("dedeuserid"),
        )

    v = video.Video(bvid=bvid, credential=cred)
    # get_download_url(cid=...) 拿 DASH 流
    data = await v.get_download_url(cid=cid)
    audios = data.get("dash", {}).get("audio", [])
    if not audios:
        raise RuntimeError(f"视频 {bvid} 没有 DASH audio stream (可能需要大会员/地区限制)")

    # 选码率最低的 audio (质量够 ASR 用, 文件最小)
    audios_sorted = sorted(audios, key=lambda a: a.get("bandwidth", 999999))
    return audios_sorted[0]["baseUrl"]


# ── 子流程 ──────────────────────────────────────────────────────────
def download_audio(bvid: str, out_dir: Path, max_duration_sec: Optional[int] = None) -> Path:
    """通过 bilibili_api 拿 DASH 音频直链 → urllib 下载 → 返回 audio.m4a 路径。
    max_duration_sec: 限制只下前 N 秒 (避免超长视频爆时间/磁盘)。

    设计选择:
      - 不用 yt-dlp: B 站 anti-bot 对 yt-dlp extractor 返回 HTTP 412
      - 直接用 bilibili_api (已验证可用) 拿签名过的直链, 走 urllib + cookie
      - 这个方案比 yt-dlp 可靠 (同一 cookie + Referer, B 站不拦截)

    失败时显式 raise (禁止 silent fallback)。
    """
    audio_path = out_dir / "audio.m4a"

    # 1. 从 B 站 API 拿 DASH 音频 URL
    cred_data = load_credential()
    cred = None
    if cred_data:
        from bilibili_api import Credential
        cred = Credential(
            sessdata=cred_data.get("sessdata"),
            bili_jct=cred_data.get("bili_jct"),
            dedeuserid=cred_data.get("dedeuserid"),
        )

    from bilibili_api import video as bili_video
    v = bili_video.Video(bvid=bvid, credential=cred)
    async def _get_cid_and_url():
        info = await v.get_info()
        cid = info["pages"][0]["cid"]
        url_data = await v.get_download_url(cid=cid)
        audios = url_data.get("dash", {}).get("audio", [])
        if not audios:
            raise RuntimeError(f"视频 {bvid} 没有 DASH audio stream")
        # 选码率最低的 (质量够 ASR, 文件最小)
        audios_sorted = sorted(audios, key=lambda a: a.get("bandwidth", 999999))
        return audios_sorted[0]["baseUrl"]

    try:
        audio_url = asyncio.run(_get_cid_and_url())
    except Exception as e:
        raise RuntimeError(f"拿 DASH URL 失败 ({type(e).__name__}): {e}")

    # 2. urllib 下载 (带 SESSDATA cookie + Referer, B 站才会放行)
    cookie = ""
    if cred_data:
        cookie = (
            f"SESSDATA={cred_data.get('sessdata', '')}; "
            f"bili_jct={cred_data.get('bili_jct', '')}; "
            f"dedeuserid={cred_data.get('dedeuserid', '')}"
        )
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Referer": f"https://www.bilibili.com/video/{bvid}",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie

    req = urllib.request.Request(audio_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            audio_bytes = resp.read()
    except Exception as e:
        raise RuntimeError(f"下载 DASH audio 失败 ({type(e).__name__}): {e}")

    if not audio_bytes:
        raise RuntimeError(f"DASH audio 下载为空 (url={audio_url[:80]}...)")

    # 3. 写到 .m4a 文件 (DASH audio segment 实际是 m4a container)
    audio_path.write_bytes(audio_bytes)

    # 4. 如果要截前 N 秒, 用 ffmpeg -t
    if max_duration_sec and max_duration_sec > 0:
        ffmpeg_bin, ffmpeg_src = find_ffmpeg()
        trimmed = out_dir / f"audio_first{max_duration_sec}s.m4a"
        cmd = [
            str(ffmpeg_bin), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(audio_path),
            "-t", str(max_duration_sec),
            "-c", "copy",  # 不重编码, 快速截取
            str(trimmed),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and trimmed.exists() and trimmed.stat().st_size > 0:
            audio_path.unlink()
            trimmed.rename(audio_path)
        else:
            # fallback: 重编码截取 (编码慢点但保险)
            cmd = [
                str(ffmpeg_bin), "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(audio_path),
                "-t", str(max_duration_sec),
                "-c:a", "aac", "-b:a", "128k",
                str(trimmed),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0 and trimmed.exists():
                audio_path.unlink()
                trimmed.rename(audio_path)

    if not audio_path.exists() or audio_path.stat().st_size == 0:
        raise RuntimeError(f"音频文件为空: {audio_path}")
    return audio_path


def find_ffmpeg() -> tuple[Path, str]:
    """优先用 imageio-ffmpeg 自带的 ffmpeg (conda 装, 不依赖系统 ffmpeg),
    fallback 到系统的 ffmpeg (PATH 找)。

    返回 (binary_path, source) — source 是 "imageio" / "system" / None。
    用户环境 ffmpeg 可能因为 brew 升级 ABI 损坏 (e.g. libx265 版本错配),
    这种情况下 imageio-ffmpeg 的 bundled ffmpeg 才是可靠的。
    """
    # 1. imageio-ffmpeg (conda 自带, 静态链接, 不依赖系统 .dylib)
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return Path(exe), "imageio-ffmpeg"
    except ImportError:
        pass

    # 2. 系统 ffmpeg (PATH 找)
    import shutil
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return Path(sys_ffmpeg), "system"

    raise RuntimeError(
        "找不到 ffmpeg。请安装: pip install imageio-ffmpeg  "
        "(conda 用户: conda install -c conda-forge imageio-ffmpeg)"
    )


def convert_to_wav(m4a_path: Path, out_dir: Path) -> Path:
    """ffmpeg 转 16kHz mono wav (FunASR 要求)。
    用 find_ffmpeg() 拿 binary, 不假设系统 ffmpeg 完好。"""
    wav_path = out_dir / "audio.wav"
    ffmpeg_bin, ffmpeg_src = find_ffmpeg()
    cmd = [
        str(ffmpeg_bin), "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(m4a_path),
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        str(wav_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg ({ffmpeg_src}) 转换失败: {proc.stderr[-500:]}"
        )
    if not wav_path.exists() or wav_path.stat().st_size == 0:
        raise RuntimeError(f"wav 文件为空: {wav_path}")
    return wav_path


def run_asr(model_name: str, wav_path: Path, out_dir: Path, lang: str) -> tuple[Path, str]:
    """调 llama-funasr-* 二进制, 返回 (srt_path, detected_lang)"""
    cfg = MODELS[model_name]
    binary = cfg["binary"]
    model = cfg["model"]
    vad = cfg["vad"]

    for p in (binary, model, vad):
        if not p.exists():
            raise RuntimeError(f"FunASR 资源缺失: {p} (请跑 download-funasr-model.sh {model_name})")

    # 文件名: auto-{lang}.srt, 但 lang="auto" (SenseVoice 默认) 时直接 auto.srt
    fname = f"auto-{lang}.srt" if lang != "auto" else "auto.srt"
    srt_path = out_dir / fname

    cmd = [
        str(binary),
        "-m", str(model),
        "--vad", str(vad),
        "-a", str(wav_path),
        "--srt",
    ]

    # stdout 是 SRT 内容 (README: "Progress and timing diagnostics remain on stderr")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"FunASR 失败 (rc={proc.returncode}): {proc.stderr[-500:]}"
        )

    srt_path.write_text(proc.stdout, encoding="utf-8")
    if srt_path.stat().st_size == 0:
        raise RuntimeError("FunASR 返回空字幕 (可能音频纯静音/全外语)")
    return srt_path, lang


def count_srt_entries(srt_path: Path) -> int:
    """数 SRT 条目数"""
    if not srt_path.exists():
        return 0
    text = srt_path.read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        return 0
    # 每个 entry 以数字开头, 用 \n\n 分隔
    return len([b for b in text.split("\n\n") if b.strip()])


def save_meta(bvid: str, meta: VideoMeta, asr_result: ASRResult):
    """写 meta.json (跟 fetch.py 格式兼容)"""
    out_dir = DOWNLOADS_DIR / bvid
    meta_path = out_dir / "meta.json"
    payload = asdict(meta)
    payload["asr"] = asdict(asr_result)
    payload["asr"]["srt_path"] = str(asr_result.srt_path)
    meta_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── 主流程 ──────────────────────────────────────────────────────────
def process_one(bvid: str, model_name: str, force: bool = False,
                 max_duration_sec: Optional[int] = None) -> ASRResult:
    """处理单个 BVID, 返回 ASRResult。失败时 ok=False + error。"""
    out_dir = DOWNLOADS_DIR / bvid
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1. 拿元数据
        meta = asyncio.run(fetch_video_meta(bvid))
        print(f"  ✓ 标题: {meta.title[:60]}")
        print(f"  ✓ 时长: {meta.duration}s / 分P: {meta.page_count}")

        # 2. 检查已有 auto-*.srt (除非 --force)
        existing = list(out_dir.glob("auto-*.srt"))
        if existing and not force:
            print(f"  ⏭️  已有 ASR 字幕, 跳过: {existing[0].name} (用 --force 覆盖)")
            return ASRResult(
                bvid=bvid, model=model_name,
                srt_path=existing[0], lang="zh",
                srt_entries=count_srt_entries(existing[0]),
                ok=True,
            )

        # 3. 下载音频
        if max_duration_sec:
            print(f"  ↓ 下载音频 (bilibili_api DASH, 前 {max_duration_sec}s)...")
        else:
            print(f"  ↓ 下载音频 (bilibili_api DASH)...")
        m4a_path = download_audio(bvid, out_dir, max_duration_sec=max_duration_sec)
        audio_bytes = m4a_path.stat().st_size
        print(f"  ✓ 音频: {m4a_path.name} ({audio_bytes/1024/1024:.1f}MB)")

        # 4. 转 wav
        print(f"  ↓ 转 16kHz mono wav (ffmpeg)...")
        wav_path = convert_to_wav(m4a_path, out_dir)
        wav_bytes = wav_path.stat().st_size
        print(f"  ✓ wav: {wav_path.name} ({wav_bytes/1024/1024:.1f}MB)")

        # 5. ASR
        print(f"  ↓ ASR ({model_name})...")
        srt_path, lang = run_asr(model_name, wav_path, out_dir, lang=MODELS[model_name]["lang_default"])
        srt_entries = count_srt_entries(srt_path)
        print(f"  ✓ SRT: {srt_path.name} ({srt_entries} 条字幕)")

        # 6. 写 meta.json
        result = ASRResult(
            bvid=bvid, model=model_name, srt_path=srt_path,
            lang=lang, audio_bytes=audio_bytes, wav_bytes=wav_bytes,
            srt_entries=srt_entries, ok=True,
        )
        save_meta(bvid, meta, result)

        # 7. 清理: 删 wav (留 m4a 供回看), 删临时 cookie
        try:
            wav_path.unlink()
        except Exception:
            pass
        cookie_file = out_dir / ".yt-cookie.txt"
        if cookie_file.exists():
            cookie_file.unlink()

        return result

    except Exception as e:
        print(f"  ✗ {bvid} 失败: {type(e).__name__}: {e}")
        return ASRResult(
            bvid=bvid, model=model_name,
            srt_path=out_dir / "auto-NONE.srt", lang="unknown",
            ok=False, error=f"{type(e).__name__}: {e}",
        )


def read_batch(path: Path) -> list[str]:
    """读批量文件, 每行一个 URL/BVID"""
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(line)
    return items


def main():
    parser = argparse.ArgumentParser(
        description="B 站无字幕视频 → 本地 FunASR 自动生成字幕",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", help="BVID 或 B 站 URL")
    parser.add_argument("--batch", type=Path, help="批量文件, 每行一个 BVID/URL")
    parser.add_argument("--model", choices=list(MODELS.keys()), default="sensevoice",
                        help="ASR 模型 (默认 sensevoice: 多语自动检测; "
                             "可选 paraformer: 中文 SOTA, 英文效果差)")
    parser.add_argument("--force", action="store_true", help="覆盖已有 auto-*.srt")
    parser.add_argument("--incremental", action="store_true",
                        help="批量模式: 跳过已有 auto-*.srt (默认行为)")
    parser.add_argument("--max-duration", type=int, default=None, metavar="SEC",
                        help="只下前 N 秒 (避免超长视频爆时间/磁盘, 测试用)")
    args = parser.parse_args()

    if not args.input and not args.batch:
        parser.error("需要 input (BVID/URL) 或 --batch <file>")
    if not FUNASR_DIR.exists():
        print(f"✗ FunASR runtime 缺失: {FUNASR_DIR}", file=sys.stderr)
        print(f"  请先解压 funasr-llamacpp-macos-arm64.tar.gz 到该目录", file=sys.stderr)
        return 1

    # 收集输入
    if args.batch:
        items = read_batch(args.batch)
        print(f"批量输入: {len(items)} 个 (来自 {args.batch})")
    else:
        url = resolve_b23(args.input)
        bvid = parse_bvid(url)
        items = [bvid]
        print(f"单视频输入: {items[0]}")

    print(f"使用模型: {args.model}")
    print(f"输出目录: {DOWNLOADS_DIR}/")
    print()

    # 逐个处理
    results = []
    for i, item in enumerate(items, 1):
        try:
            url = resolve_b23(item)
            bvid = parse_bvid(url)
        except ValueError as e:
            print(f"[{i}/{len(items)}] {item}: ✗ {e}", file=sys.stderr)
            results.append(ASRResult(bvid=item, model=args.model,
                                     srt_path=Path(), lang="unknown",
                                     ok=False, error=str(e)))
            continue

        print(f"[{i}/{len(items)}] {bvid}")
        result = process_one(
            bvid, args.model,
            force=args.force,
            max_duration_sec=args.max_duration,
        )
        results.append(result)
        print()

    # 汇总
    ok = sum(1 for r in results if r.ok)
    failed = len(results) - ok
    print("=" * 60)
    print(f"汇总: ✓ {ok} 成功 / ✗ {failed} 失败 / 总 {len(results)}")

    if results and results[0].ok and not args.batch:
        r = results[0]
        print(f"\n下一步可执行:")
        print(f"  python3 summarize.py https://www.bilibili.com/video/{r.bvid}")
        print(f"  # summarize.py 会读 downloads/{r.bvid}/auto-zh.srt 走 LLM 总结")

    if failed == 0:
        return 0
    elif ok == 0:
        return 3
    else:
        return 2


if __name__ == "__main__":
    sys.exit(main())