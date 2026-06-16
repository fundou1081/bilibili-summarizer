#!/usr/bin/env python3
"""探索跨视频洞察生成

目的: 给定 2-N 个视频的总结,生成跨视频分析报告
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from summarize import _llm_call, _get_llm_client


CROSS_VIDEO_PROMPT = """你是内容分析专家。请基于以下 {count} 个视频的总结,生成跨视频洞察报告。

要求:
1. 用 Markdown 格式
2. 包含以下板块:
   - ## 🎯 共同主题 (3-5 个)
   - ## 📈 观点演变 (按时间/主题)
   - ## 🔗 互补信息 (哪些视频互相补充)
   - ## 📋 推荐观看顺序
3. 简明扼要,不要堆砌
4. 客观中立,不要编造未提及的观点

{inputs}

请直接输出报告:"""


def build_inputs(videos):
    parts = []
    for i, v in enumerate(videos, 1):
        title = v.get("title", "")
        summary = v.get("summary", "")[:1500]
        tags = v.get("tags", [])
        tag_str = ", ".join(tags) if tags else "无"
        parts.append(f"""
### 视频 {i}: {title}
**Tags**: {tag_str}
**摘要**:
{summary}
""")
    return "\n---\n".join(parts)


def main():
    test_videos = [
        {
            "title": "Transformer 是怎么炼成的? (2023)",
            "summary": "介绍 Transformer 架构的起源、核心原理。从原始论文到 GPT-4 的演进,涵盖自注意力、位置编码、层归一化等关键概念。",
            "tags": ["Transformer", "注意力机制", "LLM", "深度学习"],
        },
        {
            "title": "LLaMA 2 训练揭秘: 7B 到 70B 的 scaling",
            "summary": "深入 Meta 的 LLaMA 2 训练细节。讨论数据配比、训练策略、RLHF 微调。重点关注 RMSNorm、SwiGLU、RoPE 等新组件。",
            "tags": ["LLaMA", "训练", "Scaling Law", "RLHF"],
        },
        {
            "title": "Mistral 7B 凭什么打败 LLaMA 2 13B?",
            "summary": "Mistral 7B 用滑动窗口注意力、Grouped Query Attention 等技术,在 7B 规模超越 13B。详细对比推理速度、显存占用。",
            "tags": ["Mistral", "GQA", "滑动窗口", "推理优化"],
        },
    ]

    cfg = _get_llm_client()
    print(f"使用模型: {cfg['model']} ({'Anthropic' if cfg['is_anthropic'] else 'OpenAI'} 格式)\n")

    inputs = build_inputs(test_videos)
    prompt = CROSS_VIDEO_PROMPT.format(count=len(test_videos), inputs=inputs)

    print("=" * 60)
    print("🔗 测试: 跨视频洞察生成")
    print("=" * 60)
    print(f"输入视频数: {len(test_videos)}")
    print(f"Prompt 长度: {len(prompt)} 字符\n")

    response = _llm_call(
        cfg,
        system="你是内容分析专家,擅长从多个视频总结中发现共性和洞察。",
        user=prompt,
        max_tokens=2000,
    )

    print("=" * 60)
    print("📊 生成的洞察报告:")
    print("=" * 60)
    print(response)


if __name__ == "__main__":
    if not os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("MINIMAX_API_KEY"):
        print("❌ 请设置 DEEPSEEK_API_KEY 或 MINIMAX_API_KEY 环境变量")
        sys.exit(1)
    main()
