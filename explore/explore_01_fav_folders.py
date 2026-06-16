#!/usr/bin/env python3
"""探索 B 站收藏夹 API (使用 bilibili_api 库)

目的: 拿到用户的收藏夹列表（不含视频），为增量导入做准备
API: get_video_favorite_list(uid)
"""

import sys
import asyncio
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bilibili_cc import load_credential
from bilibili_api import Credential
from bilibili_api.favorite_list import get_video_favorite_list


def get_cred():
    cd = load_credential()
    if not cd.get("sessdata"):
        return None
    return Credential(
        sessdata=cd.get("sessdata", ""),
        bili_jct=cd.get("bili_jct", ""),
        buvid3=cd.get("buvid3", ""),
        dedeuserid=cd.get("dedeuserid", ""),
    )


async def main():
    cred = get_cred()
    if not cred:
        print("❌ 未登录")
        return

    uid = int(cred.dedeuserid)
    print(f"✅ 用 uid={uid}\n")

    print("=" * 60)
    print("📁 测试 1: 列出用户的所有视频收藏夹")
    print("=" * 60)

    result = await get_video_favorite_list(uid=uid, credential=cred)
    print(f"返回顶层字段: {list(result.keys())}")

    # 字段路径: result['list'] 或 result['data']['list']
    folders = result.get("list", [])
    if not folders and "data" in result:
        folders = result["data"].get("list", [])

    print(f"\n找到 {len(folders)} 个视频收藏夹:\n")
    total_videos = 0
    for f in folders[:15]:
        attr = f.get("attr", 0)
        priv = "🔒" if attr & 22 else "🌐"
        cnt = f.get("media_count", 0)
        total_videos += cnt
        print(f"  {priv} 📁 {f.get('title')}")
        print(f"      id={f['id']} | {cnt} 个视频 | attr={attr}")
    if len(folders) > 15:
        print(f"  ... 还有 {len(folders) - 15} 个")
    print(f"\n📊 总视频数: {total_videos}")


if __name__ == "__main__":
    asyncio.run(main())
