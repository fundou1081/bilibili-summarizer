#!/usr/bin/env python3
"""探索超长视频的分话题处理

目的: 90 分钟以上播客/演讲,直接总结会丢细节
      用两步处理: 先切片成话题, 再按话题总结
"""

import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from summarize import _llm_call, _get_llm_client


TOPIC_SPLIT_PROMPT = """你是视频内容分析专家。给定一个长视频的字幕 (带时间戳),请识别其中的主要话题分段。

要求:
1. 输出 5-15 个话题段
2. 每段格式: [开始时间, 结束时间, 话题标题]
3. 时间用秒 (整数)
4. 话题标题简洁 (3-10 字)
5. 话题之间不要有重叠或大间隔 (>30 分钟)

输出 JSON 数组:
[
  {{"start": 0, "end": 600, "title": "开场介绍"}},
  {{"start": 600, "end": 1500, "title": "核心技术点"}},
  ...
]

视频时长: {duration} 秒
字幕前 2000 字 (样本):
{preview}

直接输出 JSON 数组:"""


TOPIC_SUMMARY_PROMPT = """基于以下视频话题片段的字幕,生成 100-200 字的总结。

话题: {topic_title} ({time_range})
关键要点要包含具体的数字、人名、案例。
不要泛泛而谈。

字幕:
{subtitle}"""


def parse_topics(text):
    text = text.strip()
    if "```" in text:
        import re
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
    try:
        topics = json.loads(text)
        if isinstance(topics, list):
            return topics
    except:
        pass
    return []


def main():
    mock_subtitle = """
0:00 开场白, 主持人介绍今天的嘉宾张教授
0:30 张教授介绍自己的研究背景
2:00 主持人: 今天的话题是 LLM 的未来
3:00 张教授: 过去 5 年 LLM 发展太快了
5:00 从 GPT-3 到 GPT-4 的能力跃迁
8:00 训练数据的规模: 从 300B tokens 到 13T
12:00 训练算力的增长: 千卡到万卡
15:00 主持人: 会不会有 scaling law 的尽头?
18:00 张教授: Chinchilla 定律的启示
22:00 数据效率 vs 模型规模
25:00 主持人: 推理成本怎么降?
28:00 张教授: 推理优化的几种方法
32:00 KV cache 的原理
36:00 投机解码
40:00 量化技术
45:00 主持人: 国产 LLM 现状?
48:00 张教授: 国产模型这两年进展
52:00 文心一言、通义千问、Kimi
56:00 国产模型的开源生态
60:00 主持人: 多模态?
63:00 张教授: 多模态是必然方向
67:00 视觉理解、音频、视频生成
72:00 具身智能
77:00 主持人: AGI 还有多远?
80:00 张教授: 个人观点
85:00 L2-L5 的分级
88:00 关键瓶颈
90:00 总结与展望
"""

    duration = 90 * 60

    cfg = _get_llm_client()
    print(f"使用模型: {cfg['model']} ({'Anthropic' if cfg['is_anthropic'] else 'OpenAI'} 格式)\n")

    print("=" * 60)
    print("🎙️ 测试 7: 超长视频分话题处理")
    print("=" * 60)
    print(f"视频时长: {duration}s ({duration//60} 分钟)")
    print(f"字幕预览长度: {len(mock_subtitle)} 字\n")

    # Step 1
    print("--- Step 1: 切分话题 ---")
    step1_prompt = TOPIC_SPLIT_PROMPT.format(
        duration=duration,
        preview=mock_subtitle,
    )
    response = _llm_call(
        cfg,
        system="你擅长分析长视频,精准切分话题。",
        user=step1_prompt,
        max_tokens=500,
    )
    print(f"LLM 响应: {response[:200]}...")
    topics = parse_topics(response)
    print(f"\n解析到 {len(topics)} 个话题:")
    for t in topics[:5]:
        print(f"  📍 [{t.get('start')}s-{t.get('end')}s] {t.get('title')}")
    if len(topics) > 5:
        print(f"  ... 共 {len(topics)} 个")

    # Step 2: 模拟逐话题总结
    print(f"\n--- Step 2: 逐话题总结 (测试前 3 个) ---")
    topic_summaries = []
    for topic in topics[:3]:
        title = topic.get("title", "")
        start = topic.get("start", 0)
        end = topic.get("end", 0)
        time_range = f"{start//60}分{start%60}秒 - {end//60}分{end%60}秒"

        mock_slice = f"[模拟 {time_range} 的字幕切片]\n张教授讨论了 {title}..."

        step2_prompt = TOPIC_SUMMARY_PROMPT.format(
            topic_title=title,
            time_range=time_range,
            subtitle=mock_slice,
        )
        try:
            summary = _llm_call(
                cfg,
                system="你是视频内容总结助手。",
                user=step2_prompt,
                max_tokens=300,
            )
            topic_summaries.append({"topic": title, "range": time_range, "summary": summary})
            print(f"  ✅ {title}: {summary[:80]}...")
        except Exception as e:
            print(f"  ❌ {title} 失败: {e}")

    # Step 3 模拟整合
    print(f"\n--- Step 3: 整合报告结构 ---")
    print("# 视频完整报告 (90 分钟)")
    print("## 目录")
    for t in topics:
        print(f"- [{t.get('start',0)//60}分{t.get('start',0)%60}秒] {t.get('title')}")
    print("## 各话题详细总结")
    for s in topic_summaries:
        print(f"\n### {s['topic']} ({s['range']})")
        print(s['summary'])

    print(f"\n✅ 长视频分话题处理流程跑通!")
    print(f"   - Step 1: 1 次 LLM 调用")
    print(f"   - Step 2: N 次 LLM 调用 (逐话题总结)")
    print(f"   - Step 3: 0 次 LLM 调用 (纯模板拼接)")


if __name__ == "__main__":
    if not os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("MINIMAX_API_KEY"):
        print("❌ 请设置 DEEPSEEK_API_KEY 或 MINIMAX_API_KEY 环境变量")
        sys.exit(1)
    main()
