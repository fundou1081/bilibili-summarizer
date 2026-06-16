#!/usr/bin/env python3
"""探索 B 站视频评论 API

API: https://api.bilibili.com/x/v2/reply
参数: type=1, oid=aid, pn, ps (max 30)
"""

import sys
import asyncio
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bilibili_cc import load_credential
from bilibili_api import comment, video, Credential
from bilibili_api.comment import Comment


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


async def fetch_comments(aid, cred, max_count=100):
    """拉评论 (含子评论), 返回 [{rpid, uname, content, like, ctime, replies: []}]"""
    all_comments = []
    page = 1
    ps = 20

    while len(all_comments) < max_count:
        try:
            # 视频评论 type=1, oid=aid
            result = await comment.get_comments(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                page_index=page,
                
                credential=cred,
            )
        except Exception as e:
            print(f"   第 {page} 页失败: {e}")
            break

        page_data = result.get("page", {})
        total = page_data.get("count", 0)
        replies = result.get("replies", []) or []

        if not replies:
            break

        for r in replies:
            cmt = {
                "rpid": r.get("rpid"),
                "uname": r.get("member", {}).get("uname", "?"),
                "level": r.get("member", {}).get("level_info", {}).get("current_level", 0),
                "content": r.get("content", {}).get("message", ""),
                "like": r.get("like", 0),
                "ctime": r.get("ctime", 0),
                "replies": [],
            }
            # 子评论
            for sub in r.get("replies", []) or []:
                cmt["replies"].append({
                    "uname": sub.get("member", {}).get("uname", "?"),
                    "content": sub.get("content", {}).get("message", ""),
                    "like": sub.get("like", 0),
                })
            all_comments.append(cmt)

        print(f"   第 {page} 页: +{len(replies)} (累计 {len(all_comments)}/{total})")

        # 没有更多了
        if len(replies) < 20:
            break
        if page >= 5:  # 安全阀: 最多 5 页 = 100 条主评论
            break
        page += 1

    return all_comments


async def main():
    cred = get_cred()
    if not cred:
        print("❌ 未登录")
        return

    print(f"✅ uid={cred.dedeuserid}\n")

    # 用之前测试过的视频: BV13pLS6vEqj (商业航天)
    test_bvid = "BV13pLS6vEqj"
    v = video.Video(bvid=test_bvid, credential=cred)
    info = await v.get_info()
    aid = info["aid"]
    title = info["title"]
    print(f"✅ 测试视频: {title}")
    print(f"   bvid={test_bvid}, aid={aid}\n")

    print("=" * 60)
    print(f"💬 测试 10: 拉视频评论")
    print("=" * 60)

    start = time.time()
    comments = await fetch_comments(aid, cred, max_count=100)
    elapsed = time.time() - start

    print(f"\n📊 拉取完成:")
    print(f"   拿到 {len(comments)} 条主评论")
    print(f"   耗时 {elapsed:.1f}s")
    total_replies = sum(len(c['replies']) for c in comments)
    print(f"   含 {total_replies} 条子评论")

    # 字段统计
    if comments:
        print(f"\n🔍 字段检查:")
        for k in ['rpid', 'uname', 'level', 'content', 'like', 'ctime', 'replies']:
            cnt = sum(1 for c in comments if c.get(k) is not None and c.get(k) != '' and c.get(k) != 0)
            print(f"   {k}: {cnt}/{len(comments)}")

    # Top 3 热门评论
    if comments:
        print(f"\n🔥 Top 3 高赞评论:")
        top = sorted(comments, key=lambda c: c['like'], reverse=True)[:3]
        for c in top:
            print(f"\n  👤 {c['uname']} (👍 {c['like']})")
            content = c['content'][:200]
            print(f"     {content}")
            if c['replies']:
                print(f"     └─ {len(c['replies'])} 条子评论")

    # 评论文本统计
    if comments:
        total_chars = sum(len(c['content']) for c in comments)
        print(f"\n📈 内容统计:")
        print(f"   总字符: {total_chars}")
        print(f"   平均: {total_chars//len(comments)} 字/评论")
        print(f"   最长: {max(len(c['content']) for c in comments)} 字")

    print(f"\n✅ #10 跑通!")


if __name__ == "__main__":
    asyncio.run(main())
