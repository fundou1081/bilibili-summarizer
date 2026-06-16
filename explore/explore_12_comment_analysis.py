#!/usr/bin/env python3
"""探索评论导出 + LLM 分析

流程:
  Step 1: 拉取视频评论 (主评论 + 热门子评论, 最多 200 条)
  Step 2: 拼接成文本
  Step 3: LLM 分析 → 常见问题 / 补充信息 / 误区纠正 / 整体情感
  Step 4: 输出 Markdown 报告

用途: 给用户提供一份"社区反馈"总结
"""

import sys
import asyncio
import time
import json
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bilibili_cc import load_credential
from bilibili_api import video, comment, Credential
from summarize import _llm_call, _get_llm_client


COMMENT_ANALYSIS_PROMPT = """你是社区洞察分析师。给定一个B站视频的评论数据，请从中提取以下维度的信息：

## 📋 输出格式

### 1. ❓ 常见问题 (3-5 个)
观众反复提到的疑问、困惑

### 2. 📝 补充信息 (3-5 个)
观众补充的上下文、额外数据、延伸知识点

### 3. ⚠️ 纠正与争议 (3-5 个)
观众指出的错误、争议观点、不同意见

### 4. 😊 整体情感
正面/中性/负面比例,以及典型反馈

### 5. 🌟 最有价值评论 (Top 3)
最值得看的评论全文

**只输出以上 5 个板块**, 不要元信息,不要"根据以下评论"前缀。

视频标题: {title}
评论总数: {total}
以下为 {taken} 条评论样本:

{text}"""


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


async def fetch_comments(aid, cred, max_pages=5):
    """拉评论, 返回文本列表"""
    all_text = []
    for page in range(1, max_pages + 1):
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
            # 子评论
            for sub in r.get("replies", []) or []:
                sub_msg = sub.get("content", {}).get("message", "").strip()
                sub_uname = sub.get("member", {}).get("uname", "?")
                if sub_msg:
                    all_text.append(f"  ↳ {sub_uname}: {sub_msg}")

        total = result.get("page", {}).get("count", 0)
        print(f"  第 {page} 页: {len(replies)} 条 (累计 {len(all_text)})")
        if len(replies) < 20:
            break

    return all_text


async def main():
    cred = get_cred()
    if not cred:
        print("❌ 未登录")
        return

    # 用股票视频 BV1tgVs6SE4C (1437 条评论)
    bvid = "BV1tgVs6SE4C"
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    print(f"✅ 视频: {info['title']}")
    print(f"   bvid={bvid}, aid={info['aid']}")
    reply_count = info.get("stat", {}).get("reply", 0)
    print(f"   评论总数: {reply_count}\n")

    print("=" * 60)
    print("💬 测试 12: 评论拉取 + LLM 分析")
    print("=" * 60)

    # Step 1: 拉评论
    print(f"\n--- Step 1: 拉评论 ---")
    start = time.time()
    texts = await fetch_comments(info["aid"], cred, max_pages=4)
    elapsed = time.time() - start
    print(f"   总耗时 {elapsed:.1f}s | 拿到 {len(texts)} 条")

    if not texts:
        print("   ❌ 没有评论可分析")
        return

    # Step 2: 拼接并截断 (LLM 输入 ~8k 字)
    combined = "\n".join(texts)
    max_input = 8000
    if len(combined) > max_input:
        combined = combined[:max_input] + "\n... (评论过长,已截断)"
    print(f"   输入 LLM 长度: {len(combined)} 字\n")

    # Step 3: LLM 分析
    print(f"--- Step 2: LLM 分析 ---")
    cfg = _get_llm_client()
    print(f"   模型: {cfg['model']} ({'Anthropic' if cfg['is_anthropic'] else 'OpenAI'})")

    prompt = COMMENT_ANALYSIS_PROMPT.format(
        title=info["title"],
        total=reply_count,
        taken=len(texts),
        text=combined,
    )

    start = time.time()
    response = _llm_call(
        cfg,
        system="你是社区洞察分析师,擅长从评论中提取有价值的信息。",
        user=prompt,
        max_tokens=2000,
    )
    elapsed = time.time() - start
    print(f"   LLM 耗时: {elapsed:.1f}s\n")

    # Step 4: 输出报告
    print("=" * 60)
    print("📊 评论分析报告:")
    print("=" * 60)
    print(response)

    print(f"\n✅ #12 跑通!")
    print(f"\n📝 整条 pipeline:")
    print(f"   1. 拉评论: {len(texts)} 条 (B 站 API)")
    print(f"   2. 拼接: {len(combined)} 字")
    print(f"   3. LLM 分析: {elapsed:.1f}s")
    print(f"   4. 输出: Markdown 报告")


if __name__ == "__main__":
    asyncio.run(main())
