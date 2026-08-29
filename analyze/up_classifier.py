#!/usr/bin/env python3
"""
B 站 UP 主分类器 (无需登录) - 基于 LLM 的元数据 + 最近视频

用法:
  # 分类指定 UID 列表
  python3 up_classifier.py --uids 3546659109735216 523635048 482374377

  # 分类 local downloads 里的 UP 主
  python3 up_classifier.py --from-local

  # 混合: 显式 + local
  python3 up_classifier.py --from-local --uids 3546659109735216

  # 输出报告
  python3 up_classifier.py --from-local -o up-classify-2026-06-29.md

  # 输出知识图谱 (PNG)
  python3 up_classifier.py --from-local --graph

  # 自定义分类
  python3 up_classifier.py --from-local --categories "科技消费,AI知识,健康,生活,投资,游戏,其他"
"""

import sys
import os
import json
import argparse
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from summarize import _llm_call, _get_llm_client

DEFAULT_DOWNLOADS = Path.home() / "my_bili_data" / "downloads"
DEFAULT_GRAPH_OUT = Path.home() / "my_bili_data" / "up_classify_graph.png"

# 关注列表 (从 local downloads 推导 + 用户给定)
DEFAULT_UIDS_TO_TRACK = [
    ("3546659109735216", "Lau博士的云组会"),
    ("523635048", "认真科普的赵博士"),
    ("482374377", "Cindy_Runxin"),
]

# 分类候选
DEFAULT_CATEGORIES = [
    "AI知识",      # AI/ML/CV/NLP/论文
    "科技消费",    # 数码/评测/开箱
    "健康",        # 医学/养生/心理
    "投资",        # 股票/基金/期货/财经
    "生活",        # 美食/咖啡/旅行
    "游戏",        # 游戏/电竞
    "教育",        # 课程/学习/语言
    "娱乐",        # 综艺/明星/八卦
    "其他",
]

# ─── LLM Prompt ─────────────────────────────────────

CLASSIFY_PROMPT = '''你是 B 站 UP 主内容分类助手。

可选分类: {categories}

根据 UP 主的元数据和最近视频标题, 判断该 UP 主属于哪个分类。

只输出一行 JSON (无 markdown, 无解释):
{{"category": "<分类>", "confidence": "<high|medium|low>", "topics": ["topic1", "topic2", "topic3"], "reason": "<20字"}}

要求:
- topics 提取 3-5 个最核心话题 (中文, 1-4 字)
- confidence: 信息充足选 high, 一般选 medium, 信息很少选 low

UP 主信息:
- UID: {uid}
- 名称: {name}
- 签名: {sign}
- 等级: {level}
- 关注数: {following}
- 粉丝数: {fans}
- 最近 {n} 个视频标题:
{videos}
'''


def build_prompt(uid: str, name: str, sign: str, level: str, following: int, fans: int, videos: List[str], categories: List[str]) -> str:
    """构建 UP 主分类 prompt"""
    video_lines = "\n".join(f"  - {t}" for t in videos)
    return CLASSIFY_PROMPT.format(
        categories=", ".join(categories),
        uid=uid,
        name=name,
        sign=sign or "(无签名)",
        level=level,
        following=following,
        fans=fans,
        n=len(videos),
        videos=video_lines,
    )


def parse_classify_json(text: str) -> Optional[Dict]:
    """解析 LLM 输出的 JSON"""
    if not text or not text.strip():
        return None
    text = text.strip()
    if "```" in text:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    return {
        "category": data.get("category", "其他"),
        "confidence": data.get("confidence", "low"),
        "topics": [str(t)[:20] for t in (data.get("topics") or [])[:5]],
        "reason": str(data.get("reason", ""))[:80],
    }


# ─── B 站 API (无需登录) ─────────────────────────────

async def fetch_up_info(uid: str) -> Optional[Dict]:
    """拉取 UP 主元数据 + 最近视频"""
    from bilibili_api import user as bili_user
    try:
        u = bili_user.User(uid=int(uid))
        # 元数据
        info = await u.get_user_info()
        # 最近视频
        videos_result = await u.get_videos()
        vlist = (videos_result or {}).get("list", {}).get("vlist", []) or []
        videos = []
        for v in vlist[:10]:
            ts = v.get("created", 0)
            videos.append({
                "title": v.get("title", "?"),
                "bvid": v.get("bvid", ""),
                "created_ts": ts,
                "created_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?",
            })
        return {
            "uid": uid,
            "name": info.get("name", "?"),
            "sign": info.get("sign", ""),
            "level": info.get("level_info", {}).get("current_level", "?"),
            "following": info.get("following", 0),
            "fans": info.get("fans", 0),
            "videos": videos,
        }
    except Exception as e:
        return {"uid": uid, "error": str(e)}


# ─── Local UP 主发现 ──────────────────────────────

def discover_local_ups(downloads_dir: Path) -> Dict[str, Dict]:
    """从 local downloads 发现所有 UP 主"""
    ups = {}
    if not downloads_dir.exists():
        return ups
    for d in sorted(downloads_dir.iterdir()):
        if not d.is_dir():
            continue
        m = d / "meta.json"
        if not m.exists():
            continue
        try:
            meta = json.loads(m.read_text(encoding="utf-8"))
        except Exception:
            continue
        mid = str(meta.get("mid", ""))
        author = meta.get("author", "?")
        if not mid or mid == "?":
            continue
        if mid not in ups:
            ups[mid] = {
                "uid": mid,
                "name": author,
                "videos": [],
            }
        ups[mid]["videos"].append({
            "title": meta.get("title", "?"),
            "bvid": d.name,
            "created_ts": meta.get("created", 0),
            "created_str": meta.get("created_str", "?"),
        })
    # 排序: 按 video 数
    return dict(sorted(ups.items(), key=lambda x: -len(x[1]["videos"])))


# ─── LLM 分类 ──────────────────────────────────────

def classify_up(client_cfg: Dict, categories: List[str], up: Dict) -> Dict:
    """LLM 分类 UP 主 (带 retry + 关键词 fallback)"""
    uid = up.get("uid", "?")
    name = up.get("name", "?")
    sign = up.get("sign", "")
    level = str(up.get("level", "?"))
    following = up.get("following", 0)
    fans = up.get("fans", 0)

    # 取最近 5 个视频标题
    videos = up.get("videos", [])
    recent_titles = []
    for v in sorted(videos, key=lambda x: -x.get("created_ts", 0))[:5]:
        recent_titles.append(f"{v.get('created_str', '?')[:10]} | {v.get('title', '?')[:60]}")
    if not recent_titles:
        recent_titles = ["(无最近视频)"]

    # 1. LLM 评估
    for attempt in range(2):
        try:
            prompt = build_prompt(uid, name, sign, level, following, fans, recent_titles, categories)
            result = _llm_call(
                client_cfg,
                system="你是 B 站 UP 主内容分类助手。只输出 JSON, 不解释。",
                user=prompt,
                max_tokens=300,
            )
            parsed = parse_classify_json(result)
            if parsed and parsed.get("category") in categories:
                parsed["_method"] = "llm"
                return parsed
        except Exception:
            continue

    # 1.5 硬编码映射 (用户明确指定的 UP 主)
    KNOWN_UPS = {
        "3546659109735216": ("AI知识", ["CV", "NLP", "论文", "云组会"], "用户指定"),
        "523635048": ("健康", ["医学", "科普"], "用户指定"),
        "482374377": ("生活", ["咖啡", "SCA Trainer"], "用户指定"),
    }
    if uid in KNOWN_UPS:
        cat, topics, note = KNOWN_UPS[uid]
        if cat in categories:
            return {
                "category": cat,
                "confidence": "high",
                "topics": topics,
                "reason": note,
                "_method": "known",
            }

    # 2. 关键词 fallback (基于 name + sign + 视频标题)
    fallback = keyword_fallback(name, sign, recent_titles, categories)
    fallback["_method"] = "fallback"
    return fallback


def keyword_fallback(name: str, sign: str, recent_titles: List[str], categories: List[str]) -> Dict:
    """基于关键词的 fallback 分类 (LLM 失败时)

    规则: 多关键词命中加权, 签名权重 1.5x (身份 > 视频)
    """
    # 关键词规则 (按优先级, 高优先级词先匹配)
    # 关键词 -> (分类, 权重)
    keyword_weights = {
        # AI 知识 (高特异度词)
        "ai": ("AI知识", 2), "人工智能": ("AI知识", 2), "大模型": ("AI知识", 2),
        "llm": ("AI知识", 2), "gpt": ("AI知识", 2), "深度学习": ("AI知识", 2),
        "机器学习": ("AI知识", 2), "nlp": ("AI知识", 2),
        "cv ": ("AI知识", 2), "computer vision": ("AI知识", 2),
        "云组会": ("AI知识", 3), "cv_nlp": ("AI知识", 3),
        # 投资 (高特异度词)
        "股市": ("投资", 2), "股票": ("投资", 2), "期货": ("投资", 2),
        "基金": ("投资", 1), "财经": ("投资", 1), "金融": ("投资", 1),
        "带粉": ("投资", 2), "回血": ("投资", 2), "冲击百万": ("投资", 3),
        "双板": ("投资", 2), "抓龙头": ("投资", 2), "神剑": ("投资", 2),
        "获利": ("投资", 2), "操盘": ("投资", 2),
        # 健康/医学 (高特异度词)
        "医学": ("健康", 2), "医生": ("健康", 2), "医院": ("健康", 2),
        "病理": ("健康", 2), "颈动脉": ("健康", 3), "降脂": ("健康", 2),
        "斑块": ("健康", 2), "血压": ("健康", 2), "血糖": ("健康", 2),
        "胰岛素": ("健康", 2), "减脂": ("健康", 2), "营养": ("健康", 1),
        "养生": ("健康", 1),
        # 游戏
        "英魂之刃": ("游戏", 3), "电竞": ("游戏", 2), "王者": ("游戏", 1),
        "lol": ("游戏", 1), "原神": ("游戏", 2), "攻略": ("游戏", 1),
        "皮肤": ("游戏", 1),
        # 科技消费
        "手机": ("科技消费", 1), "数码": ("科技消费", 2), "评测": ("科技消费", 1),
        "开箱": ("科技消费", 2), "iphone": ("科技消费", 1), "macbook": ("科技消费", 1),
        # 教育
        "课程": ("教育", 1), "教学": ("教育", 1), "老师": ("教育", 1),
        "高考": ("教育", 2), "考研": ("教育", 2), "英语": ("教育", 1),
        "留学": ("教育", 1), "公开课": ("教育", 2),
        # 生活
        "美食": ("生活", 1), "咖啡": ("生活", 2), "旅行": ("生活", 1),
        "穿搭": ("生活", 2), "美妆": ("生活", 2), "烘焙": ("生活", 2),
        "barista": ("生活", 3), "sca": ("生活", 2), "coffee": ("生活", 2),
        "sca trainer": ("生活", 3), "sca认证": ("生活", 3),
        # 娱乐
        "综艺": ("娱乐", 1), "明星": ("娱乐", 1), "八卦": ("娱乐", 1),
        "影视": ("娱乐", 1), "剧情": ("娱乐", 1), "解说": ("娱乐", 1),
    }

    sign_text = (sign or "").lower()
    video_text = " ".join(recent_titles).lower()
    name_text = (name or "").lower()
    full_text = f"{name_text} {sign_text} {video_text}"

    # 统计各分类得分
    scores = {}
    matched_kws = {}
    for kw, (cat, weight) in keyword_weights.items():
        if cat not in categories:
            continue
        if kw in full_text:
            # 签名命中加 50% 权重 (身份 > 视频)
            if kw in sign_text or kw in name_text:
                weight = int(weight * 1.5)
            scores[cat] = scores.get(cat, 0) + weight
            matched_kws.setdefault(cat, []).append(kw)

    if scores:
        # 选得分最高的
        best_cat = max(scores, key=scores.get)
        kws = matched_kws[best_cat]
        return {
            "category": best_cat,
            "confidence": "low" if scores[best_cat] < 3 else "medium",
            "topics": kws[:5],
            "reason": f"关键词: {', '.join(kws[:3])}",
        }
    return {
        "category": "其他",
        "confidence": "low",
        "topics": [],
        "reason": "无匹配关键词",
    }


# ─── 报告生成 ──────────────────────────────────────

def generate_report(categories: List[str], ups: List[Dict]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    # 按分类聚合
    by_cat = defaultdict(list)
    for u in ups:
        cat = u["classify"]["category"]
        if cat not in categories:
            cat = "其他"
        by_cat[cat].append(u)

    lines = [
        f"# UP 主分类报告 - {today}\n",
        f"## 📊 总览\n",
        f"- **UP 主数**: {len(ups)}",
        f"- **分类数**: {len(by_cat)}",
        f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
    ]
    for cat in categories:
        n = len(by_cat.get(cat, []))
        if n > 0:
            lines.append(f"- **{cat}**: {n} 个")
    lines.append("")

    # 各分类详情
    for cat in categories:
        if cat not in by_cat or not by_cat[cat]:
            continue
        lines.append(f"## 📂 {cat} ({len(by_cat[cat])} 个)\n")
        for u in by_cat[cat]:
            lines.extend(format_up(u))
        lines.append("")

    lines.append("---\n")
    lines.append(f"*Generated by up_classifier v1.0*")
    return "\n".join(lines)


def format_up(up: Dict) -> List[str]:
    info = up.get("info", {})
    classify = up["classify"]
    videos = up.get("videos", [])
    # 最近更新时间
    if videos:
        latest = max(videos, key=lambda v: v.get("created_ts", 0))
        latest_str = latest.get("created_str", "?")
        latest_title = latest.get("title", "?")[:50]
    else:
        latest_str = "?"
        latest_title = "(无)"
    topics = " ".join(f"`{t}`" for t in classify.get("topics", [])) or "—"
    lines = [
        f"### {info.get('name', '?')} (UID: {up['uid']})",
        f"- **签名**: {info.get('sign', '(无)')[:80]}",
        f"- **粉丝**: {info.get('fans', 0):,}",
        f"- **置信度**: {classify.get('confidence', '?')}",
        f"- **话题**: {topics}",
        f"- **评估**: {classify.get('reason', '?')}",
        f"- **最近更新**: {latest_str} - {latest_title}",
        f"- **链接**: https://space.bilibili.com/{up['uid']}",
    ]
    return lines


# ─── 知识图谱 ──────────────────────────────────────

def draw_graph(ups: List[Dict], output: Path):
    """画 UP 主-分类-话题 关联图"""
    try:
        import networkx as nx
        import matplotlib.pyplot as plt
        import matplotlib
        # macOS 中文字体
        for f in ["Hiragino Sans GB Interface", "STHeiti Medium", "Arial Unicode MS", "PingFang SC"]:
            try:
                matplotlib.rcParams["font.sans-serif"] = [f, "DejaVu Sans"]
                matplotlib.rcParams["axes.unicode_minus"] = False
                break
            except Exception:
                continue
    except ImportError:
        print("⚠️ 需要 pip install networkx matplotlib", file=sys.stderr)
        return

    G = nx.Graph()
    # 节点
    cat_nodes = set()
    up_nodes = set()
    topic_nodes = set()
    edges = []

    for u in ups:
        up_id = f"UP:{u['info'].get('name', '?')}"
        up_nodes.add(up_id)
        cat = u["classify"]["category"]
        cat_id = f"类:{cat}"
        cat_nodes.add(cat_id)
        edges.append((up_id, cat_id, {"weight": 1.0}))
        for t in u["classify"].get("topics", []):
            topic_id = f"话题:{t}"
            topic_nodes.add(topic_id)
            edges.append((up_id, topic_id, {"weight": 0.5}))

    # 构图
    for n in up_nodes:
        G.add_node(n, type="up")
    for n in cat_nodes:
        G.add_node(n, type="category")
    for n in topic_nodes:
        G.add_node(n, type="topic")
    for a, b, d in edges:
        G.add_edge(a, b, **d)

    # 画
    fig, ax = plt.subplots(figsize=(14, 10))
    pos = nx.spring_layout(G, k=1.5, seed=42)

    # 颜色
    color_map = []
    size_map = []
    for n in G.nodes:
        if n.startswith("UP:"):
            color_map.append("#4A90E2")  # 蓝
            size_map.append(600)
        elif n.startswith("类:"):
            color_map.append("#F5A623")  # 橙
            size_map.append(800)
        else:
            color_map.append("#7ED321")  # 绿
            size_map.append(300)

    nx.draw(
        G, pos,
        node_color=color_map,
        node_size=size_map,
        with_labels=True,
        font_size=9,
        font_weight="bold",
        edge_color="#999",
        alpha=0.85,
        ax=ax,
    )
    # 图例
    from matplotlib.lines import Line2D
    legend = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#4A90E2", markersize=12, label="UP 主"),
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#F5A623", markersize=12, label="分类"),
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#7ED321", markersize=12, label="话题"),
    ]
    ax.legend(handles=legend, loc="upper right")
    ax.set_title(f"UP 主知识图谱 - {datetime.now().strftime('%Y-%m-%d')}", fontsize=14)
    plt.tight_layout()
    plt.savefig(output, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"📊 图谱已保存: {output}", file=sys.stderr)


# ─── 主流程 ──────────────────────────────────────

async def main_async(args):
    downloads = Path(args.downloads)
    categories = [c.strip() for c in args.categories.split(",")]

    # 收集 UP 主
    ups_to_fetch: List[Dict] = []

    # 1. 从 local 推导
    if args.from_local:
        local_ups = discover_local_ups(downloads)
        for mid, info in local_ups.items():
            ups_to_fetch.append({
                "uid": mid,
                "name": info["name"],
                "videos": info["videos"],
            })
        print(f"📂 local 发现: {len(ups_to_fetch)} 个 UP 主", file=sys.stderr)

    # 2. 显式 UID
    for uid in args.uids:
        if not any(u["uid"] == uid for u in ups_to_fetch):
            ups_to_fetch.append({"uid": uid, "name": "?"})

    if not ups_to_fetch:
        print("⚠️  无 UP 主 (用 --from-local 或 --uids)", file=sys.stderr)
        return

    # 拉取 B 站 API 元数据
    print(f"🌐 拉取 B 站 API 元数据...", file=sys.stderr)
    for i, u in enumerate(ups_to_fetch, 1):
        print(f"  [{i}/{len(ups_to_fetch)}] {u['uid']}", file=sys.stderr, end="", flush=True)
        info = await fetch_up_info(u["uid"])
        if info and "error" not in info:
            u["info"] = info
            u["videos"] = u.get("videos", []) or info.get("videos", [])
            # 如果显式给了 name 但 API 给了不同 name, 用 API 的
            if u.get("name") == "?" or not u.get("name"):
                u["name"] = info.get("name", "?")
            print(f" → {info['name']} (粉丝 {info['fans']:,})", file=sys.stderr)
        else:
            # fallback: 用之前的名字 (如果有)
            u["info"] = {
                "name": u.get("name", "?"),
                "fans": 0,
                "sign": "",
                "level": "?",
            }
            err = info.get("error", "?") if info else "?"
            print(f" → 失败 ({err[:30]})", file=sys.stderr)
        # 限频
        import asyncio as _a
        await _a.sleep(1.5)

    # LLM 分类
    try:
        cfg = _get_llm_client()
        print(f"🤖 LLM: {cfg['model']}", file=sys.stderr)
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        return

    for i, u in enumerate(ups_to_fetch, 1):
        print(f"  分类 {i}/{len(ups_to_fetch)}: {u['info'].get('name', '?')}", file=sys.stderr)
        u["classify"] = classify_up(cfg, categories, u)

    # 报告
    report = generate_report(categories, ups_to_fetch)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"📝 报告: {args.output}", file=sys.stderr)
    else:
        print(report)

    # 图谱
    if args.graph:
        out = Path(args.graph) if args.graph != "graph" else DEFAULT_GRAPH_OUT
        draw_graph(ups_to_fetch, out)


def main():
    parser = argparse.ArgumentParser(description="B 站 UP 主分类器 (无需登录)")
    parser.add_argument("--from-local", action="store_true",
                        help="从 local downloads 推导 UP 主列表")
    parser.add_argument("--uids", nargs="+", default=[],
                        help="显式 UID 列表 (e.g. 3546659109735216 523635048)")
    parser.add_argument("--downloads", default=str(DEFAULT_DOWNLOADS))
    parser.add_argument("--categories",
                        default=",".join(DEFAULT_CATEGORIES),
                        help="分类列表 (逗号分隔)")
    parser.add_argument("--output", "-o", default=None)
    parser.add_argument("--graph", nargs="?", const="graph", default=None,
                        help="画知识图谱 (--graph=<path>)")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
