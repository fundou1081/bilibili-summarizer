#!/usr/bin/env python3
"""探索按 tag 聚类分组

目的: 给定一批 (video, tags),按 tag 把视频分组

方案 1 (简单): tag 字符串精确匹配分组
  - 视频 A tags: [Transformer, LLM, 深度学习]
  - 视频 B tags: [Transformer, 注意力机制]
  - → 共享 "Transformer" → 同一组

方案 2 (相似度): tag 语义相似聚类
  - 需要 embedding (Ollama nomic-embed-text / OpenAI text-embedding-3)
  - 聚类算法: HDBSCAN / KMeans

先用方案 1 跑通, 方案 2 留作可选
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

# 模拟从数据库或 LLM 提取的 tag 数据
mock_videos = [
    {"bvid": "BV1", "title": "Transformer 架构详解", "tags": ["Transformer", "LLM", "深度学习"]},
    {"bvid": "BV2", "title": "GPT-4 训练揭秘", "tags": ["GPT", "LLM", "RLHF"]},
    {"bvid": "BV3", "title": "LLaMA 2 开源解读", "tags": ["LLaMA", "开源", "LLM"]},
    {"bvid": "BV4", "title": "红烧肉做法", "tags": ["美食", "家常菜"]},
    {"bvid": "BV5", "title": "川菜入门", "tags": ["美食", "川菜", "家常菜"]},
    {"bvid": "BV6", "title": "置身事内读书笔记", "tags": ["经济", "中国", "读书"]},
    {"bvid": "BV7", "title": "货币政策解读", "tags": ["经济", "金融", "中国"]},
    {"bvid": "BV8", "title": "Transformer 注意力机制", "tags": ["Transformer", "注意力机制"]},
]


def cluster_by_tag_exact(videos):
    """方案 1: 按 tag 字符串精确匹配, 共享 tag 归同簇"""
    # tag -> set of bvid
    tag_to_videos = defaultdict(set)
    for v in videos:
        for tag in v["tags"]:
            tag_to_videos[tag].add(v["bvid"])

    # 找连通分量 (Union-Find)
    parent = {}

    def find(x):
        if x not in parent:
            parent[x] = x
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for v in videos:
        if len(v["tags"]) >= 2:
            for j in range(1, len(v["tags"])):
                union(v["tags"][0], v["tags"][j])

    # 按根 tag 分组
    cluster_videos = defaultdict(list)
    for v in videos:
        if v["tags"]:
            root = find(v["tags"][0])
            cluster_videos[root].append(v)
        else:
            cluster_videos["未分类"].append(v)

    return cluster_videos


def cluster_by_jaccard(videos, threshold=0.3):
    """方案 1.5: 用 Jaccard 相似度合并簇
    - 两个视频 tag 集合的交集/并集 > threshold → 合并
    """
    # 计算视频两两相似度
    n = len(videos)
    parent = {v["bvid"]: v["bvid"] for v in videos}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            tags_i = set(videos[i]["tags"])
            tags_j = set(videos[j]["tags"])
            if not tags_i or not tags_j:
                continue
            jaccard = len(tags_i & tags_j) / len(tags_i | tags_j)
            if jaccard >= threshold:
                union(videos[i]["bvid"], videos[j]["bvid"])

    # 按根分组
    groups = defaultdict(list)
    for v in videos:
        groups[find(v["bvid"])].append(v)
    return groups


def main():
    print("=" * 60)
    print("🏷️ 测试 8: 按 tag 聚类分组")
    print("=" * 60)
    print(f"输入视频数: {len(mock_videos)}\n")

    # 方案 1: 共享 tag 即可聚类
    print("--- 方案 1: 共享 tag 即合并 ---")
    clusters1 = cluster_by_tag_exact(mock_videos)
    for i, (tag, vs) in enumerate(clusters1.items(), 1):
        print(f"\n  簇 {i} (主 tag: {tag}):")
        for v in vs:
            print(f"    📹 {v['title']} {v['tags']}")

    # 方案 1.5: Jaccard 相似度
    print(f"\n\n--- 方案 1.5: Jaccard 相似度 (threshold=0.3) ---")
    clusters2 = cluster_by_jaccard(mock_videos, threshold=0.3)
    for i, (root, vs) in enumerate(clusters2.items(), 1):
        print(f"\n  簇 {i}:")
        for v in vs:
            print(f"    📹 {v['title']} {v['tags']}")

    # 统计
    print(f"\n\n--- 统计 ---")
    print(f"方案 1 (共享 tag):  {len(clusters1)} 个簇")
    print(f"方案 1.5 (Jaccard 0.3): {len(clusters2)} 个簇")

    # 最大簇分析
    max1 = max(clusters1.values(), key=len)
    max2 = max(clusters2.values(), key=len)
    print(f"\n方案 1 最大簇: {len(max1)} 个视频")
    print(f"方案 1.5 最大簇: {len(max2)} 个视频")

    print(f"\n✅ 简单聚类方案跑通!")
    print(f"   后续优化: 用 embedding 做语义聚类 (Jaccard 解决不了'LLM' vs '大模型' 的同义)")


if __name__ == "__main__":
    main()
