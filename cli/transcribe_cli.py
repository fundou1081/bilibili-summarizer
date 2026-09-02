#!/usr/bin/env python3
"""
transcribe_cli.py — 收藏夹转录工作流 (3 状态机: 未总结 → 总结中 → 已总结)

工作流:
  1. 扫描「未总结」收藏夹 (id=4115533556) → BVID 列表
  2. 对每个视频:
     a. 🔒 LOCK: 移到「总结中」(id=4012580756) — 防并发/丢失
     b. 用 summarize.download_subs() 下载/ASR 到 downloads/{BVID}/
     c. 把文件整理到 transcribed/{BVID}/P{N}/ (多分P) 或 transcribed/{BVID}/ (单P)
     d. 每个分P 调 summarize.summarize_one() 生成 summary.md
     e. 调 wiki_gen.main() 增量更新 wiki/
     f. 🔓 UNLOCK: 移到「已总结」(id=4090394056) — 失败留「总结中」
  3. 返回结果汇总 (含「总结中」存量)

失败处理: 转录/移动失败 → 视频留在「总结中」, 下次 --move-done 模式重试

用法:
  python3 transcribe_cli.py                          # 全自动处理 未总结 全部
  python3 transcribe_cli.py --dry-run                # 只扫描不转录
  python3 transcribe_cli.py --bvid BV1xxx            # 只处理指定视频 (测试用)
  python3 transcribe_cli.py --limit 3                # 只前 3 个
  python3 transcribe_cli.py --skip-move              # 转录完不移到已总结
  python3 transcribe_cli.py --skip-wiki              # 不更新 wiki
  python3 transcribe_cli.py --asr-max-duration 1800  # 长视频 ASR 只前 30 分钟
  python3 transcribe_cli.py --auto                    # cron 模式: 跳一切确认
  python3 transcribe_cli.py --move-done              # 只检查 + 移动 (扫总结中)
  python3 transcribe_cli.py --move-done --auto       # 自动确认后移
"""

import sys
import os
import re
import json
import shutil
import asyncio
import argparse
import subprocess
import urllib.request
from pathlib import Path
from datetime import datetime

# ─── 路径与配置 ──────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))

from bilibili_api import Credential
from bilibili_api.favorite_list import move_video_favorite_list_content

import summarize as sm  # 复用 download_subs / extract_text / summarize_one

TRANSCRIBED_DIR = PROJECT_DIR / "transcribed"
WIKI_DIR = PROJECT_DIR / "wiki"
DOWNLOADS_DIR = PROJECT_DIR / "downloads"
CRED_FILE = PROJECT_DIR / ".credential.json"

# 默认收藏夹: 「待总结」 → 「总结中」 → 「已总结」 (3 状态机)
DEFAULT_SOURCE_FAV = 4115533556   # 未总结 (待总结, 同义)
DEFAULT_IN_PROGRESS_FAV = 4012580756  # 总结中 (中转, LOCK 状态)
DEFAULT_DEST_FAV = 4090394056     # 已总结 (UNLOCK, 完成)

# 状态转换:
#   未总结 → (cron 触发) → 总结中 → (转录完成) → 已总结
#   总结中 → (转录失败) → 总结中 (下次 cron --move-done 重试)

# ─── 凭据 ────────────────────────────────────────────────────────────

def load_credential() -> Credential:
    if not CRED_FILE.exists():
        raise RuntimeError(f"凭据文件不存在: {CRED_FILE}\n先跑 bilibili_cc.py --login")
    with open(CRED_FILE) as f:
        d = json.load(f)
    return Credential(
        sessdata=d["sessdata"],
        bili_jct=d["bili_jct"],
        dedeuserid=d["dedeuserid"],
    )


# ─── 收藏夹扫描 + 移动 ──────────────────────────────────────────────

def scan_favorites(media_id: int) -> list[dict]:
    """扫描指定收藏夹, 返回 [{bvid, aid, title, duration}, ...]"""
    cred = load_credential()
    from bilibili_api.favorite_list import get_video_favorite_list
    # bilibili_api 是同步阻塞的, 在 async 上下文里跑需要 to_thread
    return asyncio.get_event_loop().run_until_complete(
        _scan_favorites_async(media_id, cred)
    ) if False else _scan_favorites_sync(media_id, cred)


def _scan_favorites_sync(media_id: int, cred: Credential) -> list[dict]:
    """同步版本: 直接循环拿完所有视频"""
    items = []
    page = 1
    while True:
        page_items = _fetch_page(media_id, cred, page, 20)
        if not page_items:
            break
        items.extend(page_items)
        if len(page_items) < 20:
            break
        page += 1
    return items


def _fetch_page(media_id: int, cred: Credential, page: int, page_size: int) -> list[dict]:
    """用原生 API 拉一页收藏夹内容 (page 从 1 开始)"""
    import requests
    url = "https://api.bilibili.com/x/v3/fav/resource/ids"
    params = {"media_id": media_id, "pn": page, "ps": page_size}
    cookies = {"SESSDATA": cred.sessdata, "bili_jct": cred.bili_jct,
               "DedeUserID": cred.dedeuserid}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
    }
    r = requests.get(url, params=params, cookies=cookies, headers=headers, timeout=10)
    try:
        data = r.json()
    except Exception:
        return []
    if data.get("code") != 0:
        return []
    # /x/v3/fav/resource/ids 只返回 aid 列表, 还需要拉详情
    # 简化: 用 /x/v3/fav/resource/list 拿详情 (需要 cookie 完整)
    url2 = "https://api.bilibili.com/x/v3/fav/resource/list"
    r2 = requests.get(url2, params=params, cookies=cookies, headers=headers, timeout=10)
    try:
        data2 = r2.json()
    except Exception:
        return []
    if data2.get("code") != 0:
        return []
    medias = (data2.get("data") or {}).get("medias") or []
    return [{
        "bvid": m.get("bvid"),
        "aid": m.get("id"),
        "title": m.get("title", ""),
        "duration": m.get("duration", 0),
    } for m in medias if m.get("bvid")]


async def move_video_to_fav(aid: int, from_fav: int, to_fav: int) -> dict:
    """把视频从 from_fav 移到 to_fav (B 站 API)"""
    cred = load_credential()
    return await move_video_favorite_list_content(
        media_id_from=from_fav,
        media_id_to=to_fav,
        aids=[aid],
        credential=cred,
    )


# ─── 核心: 转录单个视频 ──────────────────────────────────────────────

async def transcribe_one(
    bvid: str,
    asr_model: str = "sensevoice",
    asr_max_duration: int = None,
    page: int = None,
) -> tuple[bool, str]:
    """转录一个视频到 transcribed/{BVID}/
    page=None: 跑所有分P
    page=N: 只跑第N个分P (测试单个分P 用)
    返回 (success, reason)
    """
    globals()['_current_page'] = page
    url = f"https://www.bilibili.com/video/{bvid}"
    target_root = TRANSCRIBED_DIR / bvid

    # 已存在 → 跳过 (idempotent, 按 page 细粒度判断)
    if target_root.exists():
        if page is not None:
            page_summary = target_root / f"P{page}" / "summary.md"
            if page_summary.exists():
                return True, f"已转录 P{page}, 跳过"
        else:
            if any(target_root.glob("**/summary.md")):
                return True, "已转录 (全部), 跳过"

    target_root.mkdir(parents=True, exist_ok=True)

    # 强制重 ASR: 删旧 auto*.srt, 避免 bilibili_asr.py 缓存跳过
    pre_dl = DOWNLOADS_DIR / bvid
    pre_dl.mkdir(parents=True, exist_ok=True)
    for old_auto in pre_dl.glob("auto*.srt"):
        old_auto.unlink()
        print(f"  ↓ 删除旧 ASR 缓存: {old_auto.name}")

    # Step 1: 用 summarize.download_subs() 下载/ASR 到 downloads/{BVID}/
    try:
        await sm.download_subs(url, page=page)
    except Exception as e:
        return False, f"download_subs 失败: {type(e).__name__}: {e}"

    # Step 2: 整理到 transcribed/{BVID}/P{N}/ 或 transcribed/{BVID}/
    organized = await _organize_transcripts(bvid, page=page)
    if not organized:
        return False, "_organize_transcripts 返回 0 个分P"

    # Step 3: 每个分P 生成 summary.md
    summaries = _generate_summaries(bvid, target_root, page=page)
    if summaries == 0:
        return False, "_generate_summaries 返回 0 (LLM 可能失败)"

    # Step 4: 多P 视频生成 index.md
    if page is None:
        try:
            _generate_index_md(target_root)
        except Exception as e:
            print(f"  ⚠️  index.md 生成失败: {e}")

    return True, "ok"


async def _organize_transcripts(bvid: str, downloads=None, transcribed=None, page: int = None) -> int:
    """把 downloads/{BVID}/ 整理到 transcribed/{BVID}/ (单P) 或 P{N}/ (多P)
    单P 视频: 文件直接在 transcribed/{BVID}/transcript.{srt,txt}
    多P 视频: 文件在 transcribed/{BVID}/P{N}/transcript.{srt,txt}

    注: 参数 downloads/transcribed 已是 per-bvid 路径 (含 bvid), 不再加

    支持文件名: P{N}_auto.zh.srt / P{N}_transcript.srt / auto.srt / auto-zh.srt 等
    """
    if transcribed is None:
        transcribed = TRANSCRIBED_DIR / bvid
    if downloads is None:
        downloads = DOWNLOADS_DIR / bvid
    target_root = transcribed
    dl = downloads

    # 找所有 srt
    srts = sorted(dl.glob("*.srt")) if dl.exists() else []
    if not srts:
        return 0

    # 解析页码: 找 P{N}_ 或没 P{N}_ (单P) — 兼容 P1_auto.zh.srt / P1_transcript.srt
    page_nums = []
    for srt in srts:
        m = re.search(r"P(\d+)", srt.name)
        page_nums.append(int(m.group(1)) if m else 1)

    # 单P 视频 (只有 1 个 srt 或都是 page 1): 文件放根目录
    is_multi_p = len(srts) > 1 or any(p > 1 for p in page_nums)

    count = 0
    for srt, page_num in zip(srts, page_nums):
        # 测试用 page 参数: 实际语义是 "搬运上限" (搬 1..page 全部)
        # 单 P 测试 page=1 → 搬 1 个 (因为只有 1 个)
        # 多 P 测试 page=3 → 搬 3 个 (P1+P2+P3 全部)
        if page is not None and page_num > page:
            continue

        if is_multi_p:
            page_dir = target_root / f"P{page_num}"
        else:
            page_dir = target_root  # 单 P: 文件在根目录
        page_dir.mkdir(parents=True, exist_ok=True)

        # 搬 srt + 生成 txt
        target_srt = page_dir / "transcript.srt"
        shutil.copy(srt, target_srt)
        target_txt = page_dir / "transcript.txt"
        target_txt.write_text(sm.extract_text(str(target_srt)), encoding="utf-8")
        count += 1

    return count


def _resolve_title(bvid: str) -> str:
    """Auto-detect video title from:
    1. downloads/{BVID}/meta.json (cache, fast path)
    2. B站 API live call (auto-generate meta.json for next time)
    3. Fallback: bvid

    比直接用 bvid 当 title 好: LLM prompt 里 title 是有意义的语义
    (e.g. "MIT 教授讲座" vs "BV1ZNbC6fEx3"), 影响 summary 质量。
    解决 bug #7 (title fallback)。
    """
    import json
    meta_path = DOWNLOADS_DIR / bvid / "meta.json"

    # 1. meta.json cache (fast path)
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            cached = data.get("title")
            if cached:
                return cached
        except Exception:
            pass

    # 2. Live B站 API call (auto-generate cache for next time)
    try:
        from bilibili_api import video
        import asyncio
        v = video.Video(bvid=bvid)
        info = asyncio.run(v.get_info())
        title = info.get("title") or bvid
        # write cache for next run
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps({"bvid": bvid, "title": title}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  → auto-detected title: {title}")
        return title
    except Exception as e:
        print(f"  ⚠️  探测 title 失败 ({type(e).__name__}: {e}), fallback 到 bvid")

    # 3. Fallback
    return bvid


def _generate_summaries(bvid: str, transcribed: Path = None, page: int = None) -> int:
    """对 transcribed/{BVID}/ (单P) 或 P{N}/ (多P) 生成 summary.md
    transcribed 是 per-bvid 路径 (含 BVID), 不是 PROJECT_DIR/transcribed

    测试 fixture: (bvid, transcribed=Path含bvid)
    真实调用:   (bvid, transcribed=None)  → 走默认 TRANSCRIBED_DIR/bvid
    """
    if transcribed is None:
        transcribed = TRANSCRIBED_DIR / bvid
    target_root = transcribed

    # 单P: summary.md 在 transcribed/summary.md, srt 在 transcribed/transcript.srt
    # 多P: summary.md 在 transcribed/P{N}/summary.md, srt 在 transcribed/P{N}/transcript.srt
    single_summary = target_root / "summary.md"
    single_srt = target_root / "transcript.srt"
    page_dirs = sorted([p for p in target_root.iterdir() if p.is_dir() and p.name.startswith("P")])

    # 拿 title: meta.json → B站 API live call (auto-generate) → fallback bvid
    title = _resolve_title(bvid)

    count = 0
    if single_srt.exists() and not page_dirs:
        # 单P 模式: 直接在 transcribed/
        targets = [(single_summary, single_srt, None)]
    else:
        # 多P 模式: 每个 P{N}/ 一个 summary
        targets = []
        for pd in page_dirs:
            if page is not None and pd.name != f"P{page}":
                continue
            srt = pd / "transcript.srt"
            sm_path = pd / "summary.md"
            if srt.exists():
                targets.append((sm_path, srt, pd.name))

    for summary_path, srt_path, page_name in targets:
        if summary_path.exists():
            count += 1
            continue
        try:
            result = sm.summarize_one(srt_path.read_text(encoding="utf-8"), title=title)
            summary_path.write_text(result, encoding="utf-8")
            count += 1
        except Exception as e:
            placeholder = f"[失败] LLM 总结失败: {type(e).__name__}: {e}\n\n"
            placeholder += f"重跑命令: python3 transcribe_skill.py --bvid {bvid} --yes\n"
            summary_path.write_text(placeholder, encoding="utf-8")
            count += 1

    # 多P 视频生成 index.md
    if len(page_dirs) > 1:
        try:
            _generate_index_md(target_root)
        except Exception as e:
            print(f"  ⚠️  index.md 生成失败: {e}")
    return count


def _generate_index_md(target_root: Path) -> None:
    """多分P 视频生成 index.md 串起各 P
    优先用 meta.json 里的 page_names, 否则用 P{N} 标签
    """
    page_dirs = sorted([p for p in target_root.iterdir() if p.is_dir() and p.name.startswith("P")])
    if not page_dirs:
        return

    # 从 meta.json 读 page_names (如有)
    page_names = {}
    meta_path = target_root / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            for i, name in enumerate(meta.get("page_names", []) or []):
                page_names[i + 1] = name
        except Exception:
            pass

    lines = [f"# {target_root.name} 分P 索引\n"]
    for page_dir in page_dirs:
        page = page_dir.name  # "P1" / "P2" / ...
        page_num = int(page[1:]) if page[1:].isdigit() else 0
        title = page_names.get(page_num, page)
        # 也检查本地 title.txt 覆盖
        title_path = page_dir / "title.txt"
        if title_path.exists():
            title = title_path.read_text(encoding="utf-8").strip()
        summary = page_dir / "summary.md"
        if summary.exists():
            lines.append(f"- [{title}]({page}/summary.md) ({page})")
    (target_root / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_wiki() -> bool:
    """调 wiki/gen.py 同步更新 wiki/ (subprocess, timeout 120s)"""
    wiki_gen = PROJECT_DIR / "wiki" / "gen.py"
    try:
        result = subprocess.run(
            [sys.executable, str(wiki_gen), "--downloads", str(TRANSCRIBED_DIR),
             "--output", str(WIKI_DIR), "--no-summaries"],
            capture_output=True, text=True, timeout=120, cwd=str(PROJECT_DIR),
        )
        for line in result.stdout.splitlines():
            if line.startswith("[wiki]"):
                print(f"    {line}")
        if result.returncode != 0 and result.stderr:
            print(f"  ⚠️  wiki_gen stderr: {result.stderr[-500:]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("  ⚠️  wiki_gen 超时 (>120s)")
        return False
    except Exception as e:
        print(f"  ⚠️  wiki_gen 调用失败: {e}")
        return False


# ─── 主流程 ──────────────────────────────────────────────────────────

async def main_async(args):
    TRANSCRIBED_DIR.mkdir(parents=True, exist_ok=True)

    # 0a. --move-done alone: 只检查 + 移动, 跳过转录
    # 0b. --move-done + --auto (cron nightly): 先 transcribe, 后 move-done
    if args.move_done and not args.auto:
        await move_done_mode(args)
        return

    # 1. 扫描收藏夹
    print(f"\n📂 扫描收藏夹 {args.source} ...")
    items = scan_favorites(args.source)
    print(f"✓ 找到 {len(items)} 个视频")

    # --bvid 旁路: 不管收藏夹空不空, 都构造合成 item 处理
    # 用于测试 / 单视频手动触发, 不依赖收藏夹状态
    if args.bvid:
        matched = [i for i in items if i["bvid"] == args.bvid]
        if matched:
            items = matched
        else:
            print(f"⚠️  {args.bvid} 不在收藏夹 {args.source}, 构造合成 item (test 旁路模式)")
            items = [{"bvid": args.bvid, "aid": 0,
                      "title": f"(test:{args.bvid})", "duration": 0}]

    if not items:
        print("收藏夹是空的, 退出")
        return

    if args.limit:
        items = items[:args.limit]

    if args.dry_run:
        print(f"\n=== DRY RUN ===")
        for it in items:
            dur = it["duration"]
            print(f"  - {it['bvid']} ({dur // 60}min {dur % 60}s) {it['title'][:50]}")
        return

    # 2. 逐个处理
    print(f"\n🚀 处理 {len(items)} 个视频\n")

    long_items = [it for it in items if it["duration"] > args.max_duration
                  and not args.bvid]
    if long_items and not args.yes:
        print(f"⚠️  发现 {len(long_items)} 个长视频 (>{args.max_duration // 60} 分钟):")
        for it in long_items[:10]:
            mins = it["duration"] // 60
            print(f"  - {it['bvid']} ({mins}min) {it['title'][:50]}")
        if len(long_items) > 10:
            print(f"  ... 还有 {len(long_items) - 10} 个")
        print(f"\n长视频默认会 ASR 整段, 可能需要很久 + 很多磁盘。")
        print(f"建议加 --asr-max-duration 1800 (只前 30 分钟) 先试。")
        resp = input(f"\n继续跑全部? [y/N]: ").strip().lower()
        if resp != "y":
            print("已取消")
            return

    successes = []
    failures = []
    for i, it in enumerate(items, 1):
        bvid = it["bvid"]
        title = it["title"]
        dur = it["duration"]
        print(f"\n[{i}/{len(items)}] {bvid} ({dur // 60}min {dur % 60}s) {title[:50]}")

        # 步骤 0: LOCK — 先移到「总结中」
        locked = False
        if not args.skip_move:
            print(f"  → 🔒 锁定: 移到 总结中 {args.in_progress_fav} ...")
            try:
                await move_video_to_fav(it["aid"], args.source, args.in_progress_fav)
                print(f"    ✓ 已锁定")
                locked = True
            except Exception as e:
                print(f"    ⚠️  锁定失败: {type(e).__name__}: {e}")
                failures.append((it, f"锁失败 (未总结→总结中): {e}"))
                continue
        else:
            locked = True

        ok, reason = await transcribe_one(
            bvid,
            asr_model=args.asr_model,
            asr_max_duration=args.asr_max_duration,
            page=args.page,
        )

        if ok:
            successes.append(it)
            if not args.skip_wiki:
                print(f"  → 更新 wiki...")
                update_wiki()

            # 步骤 2: UNLOCK — 从「总结中」移到「已总结」
            if not args.skip_move and locked:
                print(f"  → 🔓 解锁: 移动到 已总结 {args.dest} ...")
                try:
                    await move_video_to_fav(it["aid"], args.in_progress_fav, args.dest)
                    print(f"    ✓ 已解锁移动")
                except Exception as e:
                    print(f"    ⚠️  解锁移动失败: {type(e).__name__}: {e}")
                    print(f"    📌 视频留在 总结中, 下次 --move-done 模式重试")
                    failures.append((it, f"解锁失败 (总结中→已总结): {e}"))
                    successes.pop()
        else:
            failures.append((it, reason))
            print(f"  ✗ {reason}")
            if locked and not args.skip_move:
                print(f"  📌 视频留在 总结中, 下次 --move-done 模式重试")

    # 3. 汇总
    print(f"\n{'=' * 60}")
    print(f"📊 汇总: ✓ {len(successes)} 成功 / ✗ {len(failures)} 失败 / 总 {len(items)}")
    if failures:
        print(f"\n失败的视频:")
        for it, reason in failures:
            print(f"  - {it['bvid']} {it['title'][:30]}: {reason}")

    if args.report_to:
        _write_report(
            args.report_to,
            mode="transcribe",
            items=items,
            successes=successes,
            failures=failures,
        )

    # --move-done + --auto (cron nightly): transcribe 完后扫并移动残留 orphan
    if args.move_done and args.auto:
        print(f"\n{'>' * 10} cron nightly: 转录完后再扫 总结中+待总结 跑一次 move-done")
        await move_done_mode(args)


# ─── --move-done 模式 ────────────────────────────────────────────────

async def move_done_mode(args) -> None:
    """扫描「总结中」+「待总结」收藏夹, 检查每个视频所有分P 是否都有真 summary.md。
    全部合格 → 自动 (或确认后) 移到「已总结」收藏夹。
    优先看「总结中」 (LOCK 状态), 然后看「待总结」 (catch orphan, e.g. 人工转录没走过 LOCK 流水线)。
    一个一个看, 跨源累加。
    """
    TRANSCRIBED_DIR.mkdir(parents=True, exist_ok=True)

    # 优先级: 总结中 → 待总结 (1 个 1 个看, 跨源累加)
    sources: list[tuple[str, int]] = [
        ("总结中", args.in_progress_fav),
        ("待总结", args.source),
    ]

    moved, skipped = [], []
    total_scanned = 0
    seen_bvids: set[str] = set()  # 防同一个 bvid 跨源重复处理

    for src_label, src_id in sources:
        print(f"\n📂 扫描 {src_label} {src_id} (--move-done) ...")
        items = scan_favorites(src_id)
        print(f"✓ 找到 {len(items)} 个视频")
        total_scanned += len(items)

        if not items:
            print(f"  {src_label} 是空的, 跳过")
            continue

        if args.bvid:
            items = [i for i in items if i["bvid"] == args.bvid]
            if not items:
                print(f"  {src_label} 里没找到 {args.bvid}")
                continue

        for i, it in enumerate(items, 1):
            bvid = it["bvid"]
            if bvid in seen_bvids:
                continue
            seen_bvids.add(bvid)

            title = it["title"]
            print(f"\n[{i}/{len(items)}] {bvid} {title[:40]} (源: {src_label})")

            ok, reason, parts = _check_all_summaries(bvid, title)
            if not ok:
                print(f"  ⊘ 跳过: {reason}")
                skipped.append({"item": it, "reason": reason, "source": src_label})
                continue

            print(f"  ✓ 所有分P 有真 summary.md ({len(parts)} parts)")
            for p in parts:
                print(f"    - P{p['page']} ({p['size']} bytes) {p['title'][:30]}")

            if not args.auto:
                print(f"\n  本地确认: 移到 已总结 收藏夹 {args.dest}?")
                resp = input(f"  [y/N]: ").strip().lower()
                if resp != "y":
                    print(f"  ⊘ 用户跳过")
                    skipped.append({"item": it, "reason": "用户跳过", "source": src_label})
                    continue

            # 关键修复: 从实际扫描到的源 (src_id) 移到 dest, 不是 args.source
            try:
                await move_video_to_fav(it["aid"], src_id, args.dest)
                print(f"  ✓ 已从 {src_label} 移到 已总结 {args.dest}")
                moved.append({"item": it, "parts": parts, "source": src_label})
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                print(f"  ✗ 移动失败: {err}")
                skipped.append({"item": it, "reason": f"移动失败: {err}", "source": src_label})

    print(f"\n{'=' * 60}")
    print(f"📊 --move-done: ✓ {len(moved)} 移动 / ⊘ {len(skipped)} 跳过 / 总 {total_scanned}")
    print(f"   源优先级: 总结中 ({args.in_progress_fav}) → 待总结 ({args.source})")

    if args.report_to:
        _write_report(args.report_to, mode="move_done",
                      items=[],  # move_done 模式里 items 仅作为 in_progress_count 兏底, 以 moved+skipped 为准
                      moved=moved, skipped=skipped,
                      in_progress_count=total_scanned)


# ─── 报告生成 ────────────────────────────────────────────────────────

MIN_SUMMARY_BYTES = 1500
PLACEHOLDER_MARKERS = ("[待", "[失败", "[placeholder", "[占位]", "[stub]")
SECTION_MARKERS = ("📺", "🧠", "💡", "🔑", "📐")


def _check_all_summaries(bvid: str, title: str) -> tuple[bool, str, list[dict]]:
    """检查 transcribed/{BVID}/ 下所有分P summary.md 是否真总结。
    返回 (ok, reason, parts_info)
    """
    target = TRANSCRIBED_DIR / bvid
    if not target.exists():
        return False, f"transcribed/{bvid}/ 不存在 (还没跑过)", []

    page_summaries = sorted(target.glob("P*/summary.md"))
    single_summary = target / "summary.md"

    if page_summaries:
        summaries = page_summaries
    elif single_summary.exists():
        summaries = [single_summary]
    else:
        return False, "没有 summary.md (多分P 都没跑过)", []

    parts = []
    for s in summaries:
        size = s.stat().st_size
        text = s.read_text(encoding="utf-8", errors="replace")
        if any(m.lower() in text.lower() for m in PLACEHOLDER_MARKERS):
            return False, f"{s.parent.name}/summary.md 是占位符", []
        section_count = sum(1 for m in SECTION_MARKERS if m in text)
        if section_count < 3:
            return False, f"{s.parent.name}/summary.md 不像真总结 (5 段标记只 {section_count}/5)", []
        if size < MIN_SUMMARY_BYTES:
            return False, f"{s.parent.name}/summary.md 太小 ({size} bytes < {MIN_SUMMARY_BYTES})", []

        page = s.parent.name
        parts.append({"page": page, "path": str(s), "size": size, "title": title})

    return True, "ok", parts


def _write_report(path: str, mode: str, items: list, **kw) -> None:
    """生成汇总报告 → path (HEARTBEAT 飞书推送会读它)
    mode: "transcribe" | "move_done"
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"## 📊 B站夜间转录报告 — {now}", ""]

    if mode == "transcribe":
        successes = kw.get("successes", [])
        failures = kw.get("failures", [])
        in_progress_stuck = kw.get("in_progress_stuck", [])
        lines.append(f"- **模式**: transcribe (3 状态机: 未总结 → 总结中 → 已总结)")
        lines.append(f"- **共扫描**: {len(items)} 个视频 (源: 未总结)")
        lines.append(f"- **成功**: {len(successes)} / **失败**: {len(failures)}")
        if in_progress_stuck:
            lines.append(f"- **⏸️  留在「总结中」**: {len(in_progress_stuck)} (下次 cron 重试)")
        lines.append("")
        if successes:
            lines.append("### ✅ 成功 (已完成转录 + 移到「已总结」)")
            for it in successes:
                mins = it["duration"] // 60
                lines.append(f"- `{it['bvid']}` ({mins}min) {it['title'][:50]}")
        if failures:
            lines.append("")
            lines.append("### ❌ 失败")
            for it, reason in failures:
                lines.append(f"- `{it['bvid']}` {it['title'][:40]} — {reason}")
        if in_progress_stuck:
            lines.append("")
            lines.append("### ⏸️  留在「总结中」 (转录失败, 下次重试)")
            for it, reason in in_progress_stuck:
                lines.append(f"- `{it['bvid']}` {it['title'][:40]} — {reason}")
        lines.append("")
        lines.append(f"_下次 run: 明天 03:30 AM (OpenClaw cron)_")

    elif mode == "move_done":
        moved = kw.get("moved", [])
        skipped = kw.get("skipped", [])
        in_progress_count = kw.get("in_progress_count", len(items))
        lines.append(f"- **模式**: move_done (扫「总结中」, 把合格的移到「已总结」)")
        lines.append(f"- **总结中存量**: {in_progress_count} 个视频")
        lines.append(f"- **已移到「已总结」**: {len(moved)} / **跳过**: {len(skipped)}")
        lines.append("")
        if moved:
            lines.append("### ✅ 已移到「已总结」")
            for m in moved:
                it = m["item"]
                mins = it["duration"] // 60
                n_parts = len(m["parts"])
                lines.append(f"- `{it['bvid']}` ({mins}min, {n_parts} parts) {it['title'][:50]}")
        if skipped:
            lines.append("")
            lines.append("### ⊘ 跳过 (待人工处理 / 在「总结中」重试)")
            for s in skipped:
                it = s["item"]
                lines.append(f"- `{it['bvid']}` {it['title'][:40]} — {s['reason']}")
        lines.append("")
        lines.append(f"_下次 run: 明天 03:30 AM (OpenClaw cron)_")

    content = "\n".join(lines) + "\n"
    p.write_text(content, encoding="utf-8")
    print(f"\n📝 报告已写: {p}")


# ─── CLI ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="收藏夹转录工作流 (3 状态机: 未总结 → 总结中 → 已总结)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--source", type=int, default=DEFAULT_SOURCE_FAV,
                        help=f"源收藏夹 ID (默认 {DEFAULT_SOURCE_FAV} = 未总结)")
    parser.add_argument("--in-progress-fav", type=int, default=DEFAULT_IN_PROGRESS_FAV,
                        help=f"中转收藏夹 ID (默认 {DEFAULT_IN_PROGRESS_FAV} = 总结中, LOCK 状态)")
    parser.add_argument("--dest", type=int, default=DEFAULT_DEST_FAV,
                        help=f"目标收藏夹 ID (默认 {DEFAULT_DEST_FAV} = 已总结)")
    parser.add_argument("--bvid", help="只处理指定 BVID (测试用)")
    parser.add_argument("--page", type=int, default=None, metavar="N",
                        help="只处理第 N 个分P (测试单个分P 用, 默认 = 全部)")
    parser.add_argument("--limit", type=int, help="最多处理 N 个")
    parser.add_argument("--dry-run", action="store_true", help="只扫描不转录")
    parser.add_argument("--skip-move", action="store_true", help="不移动到目标收藏夹")
    parser.add_argument("--skip-wiki", action="store_true", help="不更新 wiki")
    parser.add_argument("--asr-model", default="sensevoice", choices=["sensevoice", "paraformer"],
                        help="ASR 模型 (默认 sensevoice 多语)")
    parser.add_argument("--asr-max-duration", type=int, default=None, metavar="SEC",
                        help="长视频 ASR 只前 N 秒 (默认 None = 整段)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="跳过长视频确认提示 (谨慎)")
    parser.add_argument("--max-duration", type=int, default=1800, metavar="SEC",
                        help="超过 N 秒的视频需要确认 (默认 1800 = 30 分钟)")
    parser.add_argument("--auto", "-A", action="store_true",
                        help="全自动模式 (--yes + 跳过 --move-done 的人工确认, cron 用)")
    parser.add_argument("--move-done", action="store_true",
                        help="只检查 + 移动: 跳过转录, 检查所有分P 都有真 summary.md, "
                             "(auto 模式下) 自动移到 已总结 收藏夹")
    parser.add_argument("--report-to", metavar="PATH",
                        help="汇总报告写到指定 .md 文件 (HEARTBEAT 推飞书)")
    args = parser.parse_args()

    if args.auto:
        args.yes = True

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()