#!/usr/bin/env python3
"""探索增量导入 (跳过已下载)

目的: 用户有 500 个收藏夹视频, 一次全导太重
      需要: 增量拉取 + 跳过已存在的 BVID + 进度显示

设计:
  - 本地维护 imported_bvids.json (set of BVID)
  - 拉取收藏夹分页
  - 每页过滤掉已 imported 的
  - 显示进度: "处理 50/500 (跳过 450)"
  - 批量处理 (50个/批)

存储: 临时用 JSON 文件, 实际项目用 SQLite
"""

import os
import json
import sys
import asyncio
from pathlib import Path
from typing import Set, List

sys.path.insert(0, str(Path(__file__).parent.parent))
from bilibili_cc import load_credential
from explore_02_fav_videos import fetch_fav_videos

import aiohttp


STATE_FILE = Path(__file__).parent / "imported_bvids.json"


def load_imported() -> Set[str]:
    """加载已导入的 BVID 集合"""
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_imported(bvids: Set[str]):
    STATE_FILE.write_text(json.dumps(sorted(bvids), ensure_ascii=False, indent=2))


async def get_user_folders(uid):
    """拿用户所有收藏夹"""
    cred = load_credential()
    async with aiohttp.ClientSession(cookies={"SESSDATA": cred.get("sessdata", "")}) as session:
        async with session.get(
            "https://api.bilibili.com/x/v3/fav/folder/created/list-all",
            params={"up_mid": uid, "type": 2}
        ) as resp:
            data = await resp.json()
    if data.get("code") != 0:
        return []
    return data["data"].get("list", [])


async def fetch_all_fav_videos(media_id, on_page=None):
    """分页拉取收藏夹全部视频, 每页回调"""
    pn = 1
    all_videos = []
    while True:
        result = await fetch_fav_videos(media_id, pn=pn, ps=20)
        if result.get("code") != 0:
            break
        videos = result["data"].get("medias") or result["data"].get("media_list") or []
        if not videos:
            break
        all_videos.extend(videos)
        if on_page:
            on_page(pn, videos)
        if len(videos) < 20:
            break
        pn += 1
        if pn > 100:  # 安全阀
            break
    return all_videos


async def explore_incremental():
    cred = load_credential()
    if not cred.get("sessdata"):
        print("❌ 未登录")
        return

    sessdata = cred.get("sessdata", "")
    async with aiohttp.ClientSession(cookies={"SESSDATA": sessdata}) as session:
        async with session.get("https://api.bilibili.com/x/web-interface/nav") as resp:
            nav = await resp.json()
            if nav.get("code") != 0:
                print(f"❌ nav 失败: {nav}")
                return
            uid = nav["data"]["mid"]
            uname = nav["data"]["uname"]
            print(f"✅ 已登录: {uname} (uid={uid})\n")

    # 拿所有收藏夹
    folders = await get_user_folders(uid)
    if not folders:
        print("📭 用户没有收藏夹")
        return

    print(f"📁 找到 {len(folders)} 个收藏夹")
    for f in folders[:5]:
        print(f"  - {f['title']} ({f.get('media_count')} 个视频)")

    # 模拟: 用第一个收藏夹测试
    folder = folders[0]
    media_id = folder["id"]
    title = folder["title"]
    count = folder.get("media_count", 0)
    print(f"\n=== 增量导入测试: '{title}' (预期 {count} 个) ===\n")

    # 已导入的 BVID
    imported = load_imported()
    print(f"已导入 BVID 数量: {len(imported)}\n")

    # 分页拉取 + 增量处理
    skipped = 0
    new_videos = []
    new_imported = set()

    def on_page(pn, videos):
        nonlocal skipped
        for v in videos:
            bvid = v.get("bv_id") or v.get("bvid")
            if bvid in imported:
                skipped += 1
            else:
                new_videos.append(v)
                new_imported.add(bvid)
        print(f"  第 {pn} 页: 拿到 {len(videos)} 个, "
              f"其中 {len(videos) - (skipped - (len(videos) - len(new_videos) - (skipped - len([x for x in videos if (x.get('bv_id') or x.get('bvid')) in imported])))} 跳过, "
              f"新增 {len(videos) - [1 for x in videos if (x.get('bv_id') or x.get('bvid')) in imported].count(1)}")
        # 简化打印
        page_skipped = sum(1 for x in videos if (x.get('bv_id') or x.get('bvid')) in imported)
        page_new = len(videos) - page_skipped
        print(f"  第 {pn} 页: 跳过 {page_skipped}, 新增 {page_new}")

    all_videos = await fetch_all_fav_videos(media_id, on_page=on_page)

    print(f"\n=== 结果 ===")
    print(f"  收藏夹总视频: {len(all_videos)}")
    print(f"  跳过 (已导入): {skipped}")
    print(f"  待新增: {len(new_videos)}")
    print(f"  增量比例: {skipped / max(1, len(all_videos)) * 100:.1f}%")

    # 模拟保存
    if new_imported:
        imported.update(new_imported)
        print(f"\n💾 模拟保存: 总 {len(imported)} 个 BVID")
        print(f"   (实际项目中: 写入 SQLite, 不再需要 JSON)")

    print(f"\n✅ 增量导入流程跑通!")


if __name__ == "__main__":
    asyncio.run(explore_incremental())
