#!/usr/bin/env python3
"""评论导出 + LLM 分析 (支持模板选择)

流程:
  Step 1: 拉取指定数量的评论
  Step 2: 用选定的模板分析
  Step 3: 输出 Markdown 报告

用法:
  python3 explore_12_comment_analysis.py BV1tgVs6SE4C --count 100 --template community
"""

import sys
import asyncio
import time
import json
import argparse
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bilibili_cc import load_credential
from bilibili_api import video, comment, Credential
from summarize import (
    _llm_call,
    _get_llm_client,
    COMMENT_COMMUNITY,
    COMMENT_TECH,
    COMMENT_SENTIMENT,
)

TEMPLATES = {
    "community": ("社区洞察", COMMENT_COMMUNITY),
    "tech": ("技术纠错", COMMENT_TECH),
    "sentiment": ("舆情监控", COMMENT_SENTIMENT),
}


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


async def fetch_comments(aid, cred, count=100):
    """拉评论, 返回文本列表"""
    all_text = []
    page = 1
    while len(all_text) < count:
        try:
            result = await comment.get_comments(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                page_index=page,
                credential=cred,
            )
        except Exception as e:
            print(f"  第 {page} 页出错: {e}")
            break

        replies = result.get("replies", []) or []
        if not replies:
            break
        for r in replies:
            msg = r.get("content", {}).get("message", "").strip()
            likes = r.get("like", 0)
            uname = r.get("member", {}).get("uname", "?")
            if msg:
                all_text.append(f"👍{likes} {uname}: {msg}")
            for sub in r.get("replies", []) or []:
                sub_msg = sub.get("content", {}).get("message", "").strip()
                sub_uname = sub.get("member", {}).get("uname", "?")
                if sub_msg:
                    all_text.append(f"  ↳ {sub_uname}: {sub_msg}")

        total = result.get("page", {}).get("count", 0)
        progress = min(len(all_text), count)
        print(f"  第 {page} 页: {len(replies)} 条 (累计 {len(all_text)}/{min(total, count)})")
        if len(replies) < 20:
            break
        page += 1

    return all_text[:count]


async def main():
    parser = argparse.ArgumentParser(description="B站评论导出 + 模板分析")
    parser.add_argument("bvid", help="B站 BV 号")
    parser.add_argument("--count", type=int, default=100, help="评论条数 (默认100)")
    parser.add_argument("--template", default="community",
                        choices=["community", "tech", "sentiment"],
                        help="分析模板 (默认community)")
    parser.add_argument("--text-only", action="store_true", help="只导出评论文本，不分析")
    args = parser.parse_args()

    cred = get_cred()
    if not cred:
        print("❌ 未登录")
        return

    v = video.Video(bvid=args.bvid, credential=cred)
    info = await v.get_info()
    print(f"✅ 视频: {info['title']}")
    print(f"   bvid={args.bvid}, aid={info['aid']}")
    reply_count = info.get("stat", {}).get("reply", 0)
    print(f"   评论总数: {reply_count}\n")

    print("=" * 60)
    print(f"💬 测试 12: 评论拉取 (目标 {args.count} 条)")
    print("=" * 60)

    # Step 1: 拉评论
    start = time.time()
    texts = await fetch_comments(info["aid"], cred, count=args.count)
    elapsed = time.time() - start
    print(f"\n  总耗时 {elapsed:.1f}s | 拿到 {len(texts)} 条\n")

    if not texts:
        print("   ❌ 没有评论可分析")
        return

    if args.text_only:
        print("=== 评论文本 ===")
        for t in texts:
            print(t)
        return

    # Step 2: 拼接 + LLM 分析
    combined = "\n".join(texts)
    max_input = 8000
    if len(combined) > max_input:
        combined = combined[:max_input] + "\n... (已截断)"
    print(f"   输入 LLM 长度: {len(combined)} 字\n")

    tpl_name, tpl_content = TEMPLATES[args.template]
    cfg = _get_llm_client()
    print(f"   模型: {cfg['model']}")
    print(f"   模板: {tpl_name}\n")

    prompt = tpl_content.format(
        title=info["title"],
        total=reply_count,
        taken=len(texts),
        text=combined,
    )

    start = time.time()
    response = _llm_call(
        cfg,
        system="你是视频评论分析助手。",
        user=prompt,
        max_tokens=2000,
    )
    elapsed = time.time() - start
    print(f"   LLM 耗时: {elapsed:.1f}s\n")

    print("=" * 60)
    print("📊 评论分析报告:")
    print("=" * 60)
    print(response)


if __name__ == "__main__":
    asyncio.run(main())
