#!/usr/bin/env python3
"""
主题图: UP 主 ↔ 主题 ↔ 视频

策略:
  1. 从 1405 活跃 UP 中采样 150 个 (按分类优先级)
  2. 每个 UP 取 1-2 个最近视频 (~250 个)
  3. 下载字幕 + LLM 总结 + 关键词主题提取
  4. 画主题图

输出:
  - theme_graph.png (大图)
  - theme_report.md (报告)
  - video_summaries.json (数据)
"""

import sys
import os
import json
import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from collections import defaultdict, Counter

sys.path.insert(0, str(Path(__file__).parent))
from summarize import _llm_call, _get_llm_client
from bilibili_cc import load_credential
from up_classifier import DEFAULT_CATEGORIES, keyword_fallback

OUTPUT_DIR = Path.home() / "my_bili_data"
DOWNLOADS_DIR = OUTPUT_DIR / "downloads"

# 采样策略: 按分类分配
SAMPLING_PLAN = {
    "AI知识": 50,
    "教育": 25,
    "健康": 15,
    "科技消费": 15,
    "投资": 10,
    "生活": 10,
    "游戏": 10,
    "娱乐": 10,
    "其他": 50,
}

# ─── 采样 ───────────────────────────────────────────

def sample_ups(active: List[Dict]) -> List[Dict]:
    """按分类采样"""
    by_cat = defaultdict(list)
    for up in active:
        cat = up.get("category", "其他")
        if cat not in DEFAULT_CATEGORIES:
            cat = "其他"
        by_cat[cat].append(up)
    sampled = []
    for cat, n in SAMPLING_PLAN.items():
        items = by_cat.get(cat, [])
        # 按 mtime (关注时间) 排序, 取最新的
        items_sorted = sorted(items, key=lambda x: -x.get("mtime", 0))
        sampled.extend(items_sorted[:n])
    return sampled


# ─── 视频信息 + 字幕下载 ─────────────────────────

async def fetch_video_subtitle(bvid: str, cred=None) -> Optional[Dict]:
    """拉视频信息和字幕"""
    from bilibili_api import video as bili_video
    try:
        v = bili_video.Video(bvid=bvid, credential=cred) if cred else bili_video.Video(bvid=bvid)
        info = await v.get_info()
        cid = info.get("cid")
        duration = info.get("duration", 0)
        # 拉字幕
        transcript = ""
        try:
            sub_info = await v.get_subtitle(cid=cid)
            for sub in (sub_info.get("subtitles") or []):
                if "zh" in sub.get("lan", "").lower() or "中文" in sub.get("lan_doc", ""):
                    sub_url = sub.get("subtitle_url", "")
                    if sub_url.startswith("//"):
                        sub_url = "https:" + sub_url
                    # 下载字幕内容
                    import urllib.request
                    req = urllib.request.Request(sub_url, headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://www.bilibili.com/",
                    })
                    with urllib.request.urlopen(req, timeout=15) as r:
                        import json as _json
                        sub_data = _json.loads(r.read())
                    # 提取文本
                    parts = []
                    for item in sub_data.get("body", []):
                        parts.append(item.get("content", ""))
                    transcript = "\n".join(parts)
                    break
        except Exception:
            pass
        return {
            "bvid": bvid,
            "title": info.get("title", "?"),
            "duration": duration,
            "cid": cid,
            "transcript": transcript,
            "transcript_len": len(transcript),
        }
    except Exception as e:
        return None


# ─── LLM 主题提取 + 总结 ────────────────────────

THEME_PROMPT = '''从视频字幕中提取 3-5 个核心主题词 (1-4 字中文) 和 1 句话总结 (20 字内)。

只输出一行 JSON:
{{"themes": ["主题1", "主题2"], "summary": "<20字总结"}}

字幕:
{transcript}
'''


def extract_theme_summary(client_cfg, transcript: str) -> Dict:
    """LLM 提取主题 + 总结"""
    if not transcript or len(transcript) < 50:
        # 太短就用标题
        return {"themes": [], "summary": "(字幕太短)"}
    # 截取前 3000 字
    text = transcript[:3000]
    prompt = THEME_PROMPT.format(transcript=text)
    for attempt in range(2):
        try:
            result = _llm_call(
                client_cfg,
                system="只输出 JSON。",
                user=prompt, max_tokens=200,
            )
            m = re.search(r"\{.*?\}", result, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                themes = data.get("themes", [])
                summary = data.get("summary", "")
                if isinstance(themes, list):
                    themes = [str(t)[:15] for t in themes[:5]]
                else:
                    themes = []
                return {
                    "themes": themes,
                    "summary": str(summary)[:80] if summary else "",
                }
        except Exception:
            continue
    return {"themes": [], "summary": ""}


# ─── 主题图 ───────────────────────────────────────

def draw_theme_graph(data: Dict, output: Path):
    """画 UP 主-主题-视频 主题图"""
    try:
        import networkx as nx
        import matplotlib.pyplot as plt
        import matplotlib
        for f in ["Hiragino Sans GB Interface", "STHeiti Medium"]:
            try:
                matplotlib.rcParams["font.sans-serif"] = [f, "DejaVu Sans"]
                matplotlib.rcParams["axes.unicode_minus"] = False
                break
            except Exception:
                continue
    except ImportError:
        print("⚠️ 需要 networkx matplotlib")
        return

    G = nx.Graph()
    up_nodes = set()
    theme_nodes = set()
    video_nodes = set()
    edges = []

    for item in data["videos"]:
        up_id = f"UP:{item['up_name']}"
        video_id = f"视频:{item['title'][:15]}"
        up_nodes.add(up_id)
        video_nodes.add(video_id)
        edges.append((up_id, video_id, 1.5))
        for t in item.get("themes", []):
            theme_id = f"主题:{t}"
            theme_nodes.add(theme_id)
            edges.append((video_id, theme_id, 0.8))

    for n in up_nodes:
        G.add_node(n, type="up")
    for n in video_nodes:
        G.add_node(n, type="video")
    for n in theme_nodes:
        G.add_node(n, type="theme")
    for a, b, w in edges:
        G.add_edge(a, b, weight=w)

    # 主题大小按频次
    theme_count = Counter()
    for item in data["videos"]:
        for t in item.get("themes", []):
            theme_count[t] += 1

    fig, ax = plt.subplots(figsize=(32, 24))
    pos = nx.spring_layout(G, k=2.0, seed=42, weight="weight")
    color_map = []
    size_map = []
    for n in G.nodes:
        if n.startswith("UP:"):
            color_map.append("#4A90E2")
            size_map.append(150)
        elif n.startswith("视频:"):
            color_map.append("#F5A623")
            size_map.append(80)
        else:
            # 主题大小按频次
            t = n.replace("主题:", "")
            c = theme_count.get(t, 1)
            color_map.append("#7ED321")
            size_map.append(80 + c * 30)

    nx.draw(G, pos, node_color=color_map, node_size=size_map,
            with_labels=True, font_size=5, font_weight="bold",
            edge_color="#ccc", alpha=0.85, ax=ax)
    from matplotlib.lines import Line2D
    legend = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#4A90E2", markersize=10, label="UP 主"),
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#F5A623", markersize=10, label="视频"),
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#7ED321", markersize=10, label="主题"),
    ]
    ax.legend(handles=legend, loc="upper right")
    ax.set_title(f"主题图谱 - {data['total_videos']} 个视频 / {len(theme_count)} 个主题 - {datetime.now().strftime('%Y-%m-%d')}", fontsize=14)
    plt.tight_layout()
    plt.savefig(output, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"📊 主题图: {output}")


# ─── 主流程 ───────────────────────────────────────

async def main_async():
    print("=" * 60, flush=True)
    print("🚀 主题图: UP 主 ↔ 主题 ↔ 视频", flush=True)
    print("=" * 60, flush=True)

    # 1. 加载活跃 UP
    active_path = OUTPUT_DIR / "active_ups_classified.json"
    if not active_path.exists():
        print("❌ 没找到 active_ups_classified.json, 先跑 up_classify_active.py")
        return
    active = json.loads(active_path.read_text())
    print(f"加载 {len(active)} 个活跃 UP", flush=True)

    # 2. 采样
    sampled = sample_ups(active)
    print(f"采样 {len(sampled)} 个 UP (按分类优先级)", flush=True)
    for cat, n in SAMPLING_PLAN.items():
        print(f"  {cat}: {n}", flush=True)

    # 3. 选视频 (每个 UP 1-2 个)
    tasks = []
    for up in sampled:
        recent = up.get("recent_videos", [])
        for v in recent[:2]:  # 每个 UP 取 2 个
            tasks.append({
                "up_mid": up["mid"],
                "up_name": up["name"],
                "category": up.get("category", "其他"),
                "bvid": v["bvid"],
                "title": v["title"],
                "created_str": v.get("created_str", "?"),
            })
    print(f"待处理视频: {len(tasks)}", flush=True)

    # 4. 下载字幕
    print("下载字幕 (限频)...", flush=True)
    # 用 credential 减少风控
    from bilibili_api import Credential
    cred = Credential(
        sessdata=load_credential().get("sessdata", ""),
        bili_jct=load_credential().get("bili_jct", ""),
        buvid3=load_credential().get("buvid3", "") or "",
        dedeuserid=load_credential().get("dedeuserid", ""),
    )
    items = []
    for i, t in enumerate(tasks, 1):
        bvid = t["bvid"]
        info = await fetch_video_subtitle(bvid, cred=cred)
        if info and info.get("transcript"):
            t["transcript"] = info["transcript"]
            t["transcript_len"] = info["transcript_len"]
            t["duration"] = info.get("duration", 0)
        else:
            t["transcript"] = ""
            t["transcript_len"] = 0
        items.append(t)
        if i % 20 == 0:
            n_with = sum(1 for x in items if x.get("transcript_len", 0) > 0)
            print(f"  [{i}/{len(tasks)}] 有字幕: {n_with}", flush=True)
        await asyncio.sleep(0.5)

    n_with_sub = sum(1 for x in items if x.get("transcript_len", 0) > 0)
    print(f"✅ 有字幕: {n_with_sub}/{len(items)} ({n_with_sub/max(1,len(items))*100:.0f}%)", flush=True)

    # 5. LLM 主题 + 总结 (只对有字幕的)
    print("LLM 主题提取 + 总结...", flush=True)
    try:
        cfg = _get_llm_client()
    except Exception as e:
        print(f"❌ {e}")
        return

    for i, item in enumerate(items, 1):
        if item.get("transcript_len", 0) < 50:
            item["themes"] = []
            item["summary"] = "(无字幕)"
            continue
        result = extract_theme_summary(cfg, item["transcript"])
        item["themes"] = result["themes"]
        item["summary"] = result["summary"]
        if i % 10 == 0:
            print(f"  LLM {i}/{len(items)}", flush=True)
        await asyncio.sleep(1.5)

    # 6. 报告
    print("生成报告...", flush=True)
    today = datetime.now().strftime("%Y-%m-%d")
    theme_counter = Counter()
    for item in items:
        for t in item.get("themes", []):
            theme_counter[t] += 1

    lines = [
        f"# 主题图报告 - {today}",
        "",
        "## 📊 总览",
        f"- **采样 UP 主**: {len(sampled)}",
        f"- **处理视频**: {len(items)}",
        f"- **有字幕**: {n_with_sub}",
        f"- **独立主题**: {len(theme_counter)}",
        "",
        "## 🔥 Top 20 主题",
        "",
    ]
    for theme, cnt in theme_counter.most_common(20):
        lines.append(f"- **{theme}**: {cnt} 个视频")

    # 按主题展示视频
    lines.append("")
    lines.append("## 📹 视频分类展示")
    lines.append("")
    for theme, _ in theme_counter.most_common(30):
        videos = [i for i in items if theme in i.get("themes", [])]
        if not videos:
            continue
        lines.append(f"### 📌 {theme} ({len(videos)} 个)")
        lines.append("")
        for v in videos[:5]:
            lines.append(f"- [{v['up_name']}] {v['title'][:60]}")
            if v.get("summary"):
                lines.append(f"  - 💡 {v['summary']}")
        if len(videos) > 5:
            lines.append(f"- ...及其他 {len(videos) - 5} 个")
        lines.append("")

    lines.append("---")
    lines.append("*Generated by theme_graph v1.0*")

    out_md = OUTPUT_DIR / "theme_report.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告: {out_md}", flush=True)

    # 数据
    data_out = OUTPUT_DIR / "video_summaries.json"
    summary_data = {
        "total_videos": len(items),
        "total_themes": len(theme_counter),
        "videos": [
            {
                "up_name": i["up_name"],
                "up_mid": i["up_mid"],
                "category": i.get("category", ""),
                "bvid": i["bvid"],
                "title": i["title"],
                "summary": i.get("summary", ""),
                "themes": i.get("themes", []),
                "duration": i.get("duration", 0),
            }
            for i in items
        ],
    }
    data_out.write_text(json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"数据: {data_out}", flush=True)

    # 7. 图
    print("画主题图...", flush=True)
    out_png = OUTPUT_DIR / "theme_graph.png"
    await asyncio.to_thread(draw_theme_graph, summary_data, out_png)

    return summary_data


if __name__ == "__main__":
    import matplotlib
    matplotlib.use("Agg")
    asyncio.run(main_async())