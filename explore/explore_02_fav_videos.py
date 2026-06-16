#!/usr/bin/env python3
"""探索收藏夹视频分页 (使用 bilibili_api 库)

API: get_video_favorite_list_content(media_id, page)
"""

import sys
import asyncio
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bilibili_cc import load_credential
from bilibili_api import Credential
from bilibili_api.favorite_list import get_video_favorite_list, get_video_favorite_list_content


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


async def fetch_all_videos(media_id, cred):
    """分页拉完收藏夹所有视频, 返回 [(bvid, title, up, duration, mtime), ...]"""
    all_videos = []
    page = 1
    while True:
        try:
            result = await get_video_favorite_list_content(
                media_id=media_id,
                page=page,
                credential=cred,
            )
        except Exception as e:
            print(f"   第 {page} 页失败: {e}")
            break

        # 解析响应
        medias = result.get("medias") or []
        info = result.get("info", {})
        total = info.get("media_count", 0)

        if not medias:
            break

        for m in medias:
            all_videos.append({
                "bvid": m.get("bv_id") or m.get("bvid"),
                "title": m.get("title", ""),
                "up": m.get("upper", {}).get("name", "?"),
                "up_mid": m.get("upper", {}).get("mid"),
                "duration": m.get("duration", 0),
                "mtime": m.get("mtime", 0),
                "ctime": m.get("ctime", 0),
                "fav_time": m.get("fav_time", 0),
            })

        if total:
            print(f"   第 {page} 页: +{len(medias)} (累计 {len(all_videos)}/{total})")
        else:
            print(f"   第 {page} 页: +{len(medias)} (累计 {len(all_videos)})")

        if not medias or len(medias) < 20:
            break
        page += 1
        if page > 200:  # 安全阀
            break

    return all_videos


async def main():
    cred = get_cred()
    if not cred:
        print("❌ 未登录")
        return

    uid = int(cred.dedeuserid)
    print(f"✅ uid={uid}\n")

    # 拿收藏夹列表
    folders_result = await get_video_favorite_list(uid=uid, credential=cred)
    folders = folders_result.get("list", [])

    # 选默认收藏夹
    default_folder = folders[0]
    media_id = default_folder["id"]
    title = default_folder["title"]
    count = default_folder.get("media_count", 0)
    print("=" * 60)
    print(f"📁 测试 2: 拿收藏夹 '{title}' 的所有视频")
    print("=" * 60)
    print(f"   media_id={media_id}, 预期 {count} 个\n")

    start = time.time()
    videos = await fetch_all_videos(media_id, cred)
    elapsed = time.time() - start

    print(f"\n📊 拉取完成:")
    print(f"   拿到 {len(videos)} 个视频")
    print(f"   耗时 {elapsed:.1f}s")
    print(f"   速率: {len(videos)/max(0.1, elapsed):.1f} 个/秒")

    if videos:
        print(f"\n前 5 个:")
        for v in videos[:5]:
            print(f"  📹 {v['title'][:40]}")
            print(f"     bvid={v['bvid']} | up={v['up']} | {v['duration']}s")

        # 时长分析
        total_dur = sum(v['duration'] for v in videos)
        print(f"\n📈 视频时长分析:")
        print(f"   总时长: {total_dur/3600:.1f} 小时")
        print(f"   平均: {total_dur/len(videos):.0f} 秒/视频")

        # 字段检查 - 确认哪些有用
        print(f"\n🔍 字段可用性:")
        for k in ['bvid', 'title', 'up', 'up_mid', 'duration', 'mtime', 'ctime', 'fav_time']:
            count_present = sum(1 for v in videos if v.get(k) is not None and v.get(k) != 0 and v.get(k) != '')
            print(f"   {k}: {count_present}/{len(videos)} ({count_present/max(1,len(videos))*100:.0f}%)")


if __name__ == "__main__":
    asyncio.run(main())
