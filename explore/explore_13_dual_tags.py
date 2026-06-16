#!/usr/bin/env python3
"""探索双 Tag 系统：AI tag + 原始 tag

目标:
  1. 给视频生成 AI tag (复用 explore_05)
  2. 拿 B 站原始 tag (API 中的 video_tag)
  3. 实现聚类的 tag 来源选择 (原始 | AI | 混合)

验证:
  - B 站 API 是否返回视频 tag
  - AI tag 生成是否稳定
  - 双 tag 合并后的聚类效果
"""

import sys
import asyncio
import json
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bilibili_cc import load_credential
from bilibili_api import video, Credential
from summarize import _llm_call, _get_llm_client
from explore_05_tag_extraction import build_prompt, parse_tags


TAG_COMPARE_PROMPT = """你是一个视频分类专家。对比视频的原始标签和 AI 标签,找出差异。

原始标签: {original}
AI 标签:   {ai}

视频标题: {title}

请简要分析:
1. 哪个标签更准确/更有用?
2. AI 标签补充了哪些原始标签没有的维度?
3. 混合使用 (并集∪) 是否能更好地表达视频?

一句话回答:"""


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

    # 用 2 个视频测试
    test_bvids = [
        "BV13pLS6vEqj",  # 商业航天
        "BV17XVq6zEnD",  # MacBook M5
    ]

    cfg = _get_llm_client()
    results = []

    for bvid in test_bvids:
        print("=" * 60)
        print(f"📹 测试视频: {bvid}")
        print("=" * 60)

        v = video.Video(bvid=bvid, credential=cred)
        info = await v.get_info()
        title = info["title"]
        print(f"   标题: {title}")

        # 1. 拿原始 tag (B 站 API)
        original_tags = []
        raw_tags_field = info.get("tag")
        if raw_tags_field and isinstance(raw_tags_field, str):
            original_tags = [t.strip() for t in raw_tags_field.split(",") if t.strip()]
        else:
            raw_list = info.get("tags", [])
            if raw_list:
                original_tags = [(t.get("tag_name") or t.get("name") or str(t)) for t in raw_list]
        print(f"   原始 tag: {original_tags}")

        # 2. 拿视频摘要(模拟 - 用 desc 字段)
        desc = info.get("desc", "")[:500]
        print(f"   描述: {desc[:80]}...")

        # 3. 生成 AI tag
        prompt = build_prompt(title, desc)
        response = _llm_call(
            cfg,
            system="你是视频内容分析专家,擅长从内容中提取精准标签。",
            user=prompt,
            max_tokens=500,
        )
        ai_tags = parse_tags(response)
        print(f"   AI tag:  {ai_tags}")

        # 4. 混合标签 (并集)
        mixed_tags = list(set(original_tags + ai_tags))
        print(f"   混合 tag: {mixed_tags}")

        # 5. LLM 对比评价
        compare_prompt = TAG_COMPARE_PROMPT.format(
            original=", ".join(original_tags) if original_tags else "(无)",
            ai=", ".join(ai_tags),
            title=title,
        )
        # 用短响应
        compare = _llm_call(cfg, system="你是视频分类专家。", user=compare_prompt, max_tokens=200)
        print(f"   LLM 对比评价: {compare[:150]}")

        results.append({
            "bvid": bvid,
            "title": title,
            "original_tags": original_tags,
            "ai_tags": ai_tags,
            "mixed_tags": mixed_tags,
        })
        print()

    # 6. 展示三种聚类模式
    print("=" * 60)
    print("🏷️ 测试 13: 双 Tag 聚类效果")
    print("=" * 60)

    for mode, key in [("仅原始 tag", "original_tags"), ("仅 AI tag", "ai_tags"), ("混合 tag", "mixed_tags")]:
        print(f"\n--- 模式: {mode} ---")
        # 直接按共享 tag 分组
        from collections import defaultdict
        tag_to_videos = defaultdict(list)
        for r in results:
            for tag in r[key]:
                tag_to_videos[tag].append(r["title"][:30])
        # 显示分组
        clusters = defaultdict(list)
        for tag, titles in tag_to_videos.items():
            if len(titles) >= 2:  # 共享 ≥2 个视频才叫簇
                clusters[tag] = titles
        if clusters:
            for tag, titles in clusters.items():
                print(f"   🏷️ {tag}:")
                for t in titles:
                    print(f"      📹 {t}")
        else:
            print(f"   (2 个测试视频不足以形成簇)")

    print(f"\n✅ #13 跑通!")
    print(f"\n📝 数据模型建议:")
    print(f"   videos 表加字段:")
    print(f"     original_tags  TEXT (JSON 数组)")
    print(f"     ai_tags        TEXT (JSON 数组)")
    print(f"   聚类时:")
    print(f"     选择'原始': 只用 original_tags")
    print(f"     选择'AI':   只用 ai_tags")
    print(f"     选择'混合': 用 original_tags ∪ ai_tags")


if __name__ == "__main__":
    asyncio.run(main())
