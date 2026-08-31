#!/usr/bin/env python3
"""
Retry transcribe for 3 videos locked in 总结中 (4012580756) after B1 dry-run LLM failed.

跳过 LOCK (已在总结中), 直接调 transcribe_one, 然后 move 到 已总结 (4090394056).

用法:
  python3 retry_transcribe_3videos.py
"""
import asyncio
import os
import sys
from pathlib import Path

# 让脚本能找到 cli/ 和 core/
PROJECT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / 'cli'))

# 加载 .env
env_path = PROJECT_DIR / '.env'
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ[k.strip()] = v.strip()

from bilibili_api import Credential
from cli.transcribe_cli import transcribe_one, move_video_to_fav, load_credential, DEFAULT_DEST_FAV
from cli.transcribe_cli import update_wiki as _update_wiki

IN_PROGRESS_FAV = 4012580756  # 总结中
DEST_FAV = 4090394056          # 已总结

BVIDS = ["BV1ZNbC6fEx3", "BV1gSVm6HEuA", "BV1midrBNE2m"]


async def process_one(bvid: str) -> bool:
    """跑 transcribe_one (无 LOCK), 成功后 UNLOCK 到 已总结."""
    print(f"\n{'=' * 60}")
    print(f"📝 {bvid}")
    print(f"{'=' * 60}")
    try:
        # 跳过 LOCK (video 已在 总结中). 直接转录.
        ok, reason = await transcribe_one(
            bvid,
            asr_model="sensevoice",
            asr_max_duration=0,
            page=None,
        )
        if not ok:
            print(f"  ✗ transcribe 失败: {reason}")
            return False

        print(f"  ✓ transcribe 成功")

        # 更新 wiki
        try:
            _update_wiki()
            print(f"  ✓ wiki 更新")
        except Exception as e:
            print(f"  ⚠️  wiki 更新失败: {e}")

        # 找 aid (用 transcribe_one 的 meta 已经写过 meta.json)
        from core.bilibili_api import fetch_video_meta_async
        meta = await fetch_video_meta_async(bvid)
        aid = meta.get('aid')
        if not aid:
            print(f"  ✗ 找不到 aid, 不能 UNLOCK")
            return False

        # UNLOCK: 总结中 → 已总结
        cred = load_credential()
        await move_video_to_fav(aid, IN_PROGRESS_FAV, DEST_FAV)
        print(f"  ✓ 已 UNLOCK 到 已总结 {DEST_FAV}")
        return True

    except Exception as e:
        print(f"  ✗ 异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    print(f"🚀 Retry transcribe 3 videos (LOCKED in 总结中 {IN_PROGRESS_FAV})")
    print(f"   BVIDS: {BVIDS}")
    print(f"   skip LOCK (已锁), 直接走 transcribe_one + UNLOCK")
    print()

    # 顺序跑 (避免 LLM rate limit)
    results = []
    for bvid in BVIDS:
        ok = await process_one(bvid)
        results.append((bvid, ok))

    print(f"\n{'=' * 60}")
    print(f"📊 最终:")
    for bvid, ok in results:
        marker = '✓' if ok else '✗'
        print(f"   {marker} {bvid}")
    success = sum(1 for _, ok in results if ok)
    print(f"\n成功: {success} / 失败: {len(results) - success}")


if __name__ == "__main__":
    asyncio.run(main())