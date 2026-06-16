#!/usr/bin/env python3
"""探索 LLM 自动给视频生成 tag

目的: 测试用 LLM 从视频总结提取 3-5 个标签,用于 #4 按 tag 分组

支持模型: MiniMax M2.7 (Anthropic 格式) 或 DeepSeek (OpenAI 格式)
"""

import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from summarize import _llm_call, _get_llm_client


TAG_EXTRACTION_PROMPT = """从以下视频内容中提取 3-5 个精准标签,用于分类和检索。

要求:
1. 标签用中文,1-4 字
2. 覆盖: 技术领域、人物、主题、行业 等维度
3. 不要泛词 (如 "教程"、"分享"、"知识")
4. 输出 JSON 数组,例如: ["AI", "大模型", "深度学习", "Transformer", "训练优化"]

视频标题: {title}

视频内容:
{content}

只输出 JSON 数组,不要其他内容:"""


def build_prompt(title, content):
    truncated = content[:4000] if len(content) > 4000 else content
    return TAG_EXTRACTION_PROMPT.format(title=title, content=truncated)


def parse_tags(text):
    text = text.strip()
    if "```" in text:
        import re
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
    try:
        tags = json.loads(text)
        if isinstance(tags, list):
            return [str(t).strip() for t in tags if t][:5]
    except:
        pass
    return []


def main():
    test_videos = [
        {
            "title": "Transformer 是怎么炼成的? 从注意力机制到 GPT-4 的进化史",
            "content": """本视频详细介绍了 Transformer 架构的起源、核心原理,
以及从原始论文到 GPT-4 的演进过程。涵盖了自注意力机制、位置编码、
多头注意力、层归一化等关键概念,以及 BERT、GPT、LLaMA 等代表性模型。
最后讨论了 LLM 训练中的 scaling law 和推理优化技术。""",
        },
        {
            "title": "三小时读完《置身事内》— 中国政府与经济发展",
            "content": """兰小欢教授的《置身事内》是一本关于中国政府与经济发展的书。
本书从地方政府的视角出发,分析了中国式财政关系的形成与演变,
包括分税制改革、土地财政、城投债、地方融资平台等关键议题。
视频以通俗易懂的方式解读了这些复杂的政策话题。""",
        },
        {
            "title": "10分钟学会做红烧肉, 厨房小白也能变大厨",
            "content": """本视频教大家做经典红烧肉。需要准备五花肉、冰糖、老抽、生抽、
料酒、八角、桂皮等食材。步骤包括: 焯水去腥、炒糖色、上色、焖煮40分钟。
视频详细讲解了火候控制和调味比例,让厨房新手也能做出饭店级别的红烧肉。""",
        },
    ]

    cfg = _get_llm_client()
    print(f"使用模型: {cfg['model']} ({'Anthropic' if cfg['is_anthropic'] else 'OpenAI'} 格式)\n")

    for video in test_videos:
        prompt = build_prompt(video["title"], video["content"])
        print("=" * 60)
        print(f"📹 测试: {video['title']}")
        print("=" * 60)
        try:
            response = _llm_call(
                cfg,
                system="你是一个视频内容分析专家,擅长从内容中提取精准标签。",
                user=prompt,
                max_tokens=500,
            )
            print(f"LLM 输出: {response}")
            tags = parse_tags(response)
            print(f"解析后 tags: {tags}")
            print()
        except Exception as e:
            print(f"❌ LLM 调用失败: {e}")
            return


if __name__ == "__main__":
    if not os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("MINIMAX_API_KEY"):
        print("❌ 请设置 DEEPSEEK_API_KEY 或 MINIMAX_API_KEY 环境变量")
        sys.exit(1)
    main()
