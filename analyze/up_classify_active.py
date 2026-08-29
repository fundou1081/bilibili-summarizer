#!/usr/bin/env python3
"""
B 站活跃 UP 主 + 视频主题图谱

策略:
  1. 从 2876 关注中筛出"最近 1 个月有更新"的 (200-400 个)
  2. 关键词粗分 + 拉最近 5 个视频提取主题
  3. LLM 精分 30-50 个 (上限)
  4. 构建大图: UP主 ↔ 分类 ↔ 视频主题

耗时估算:
  - 拉 2876 个 UP 主的最新视频 ~30 分钟 (1s 间隔)
  - 关键词粗分: < 1 秒
  - LLM 精分 50 个: ~5 分钟
  - 画图: < 1 分钟
"""

import sys
import os
import json
import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Set
from collections import defaultdict, Counter

sys.path.insert(0, str(Path(__file__).parent))
from summarize import _llm_call, _get_llm_client
from bilibili_cc import load_credential
from up_classifier import (
    DEFAULT_CATEGORIES, keyword_fallback, classify_up,
    CLASSIFY_PROMPT, build_prompt, parse_classify_json,
)

OUTPUT_DIR = Path.home() / "my_bili_data"
FOLLOWINGS_CACHE = OUTPUT_DIR / "followings_cache.json"
ACTIVE_CACHE = OUTPUT_DIR / "active_ups_cache.json"

# 时间窗: 30 天
ONE_MONTH_AGO_TS = int((datetime.now() - timedelta(days=30)).timestamp())


# ─── 拉关注列表 ──────────────────────────────────────

async def fetch_all_followings(uid: int, sessdata: str, bili_jct: str) -> List[Dict]:
    """从缓存读关注列表 (之前已缓存)"""
    if FOLLOWINGS_CACHE.exists():
        data = json.loads(FOLLOWINGS_CACHE.read_text())
        return data["ups"]
    # 否则重新拉
    import aiohttp
    headers = {
        "User-Agent": "Mozilla/5.0 Chrome/120 Safari/537.36",
        "Referer": "https://space.bilibili.com/",
        "Cookie": f"SESSDATA={sessdata}; bili_jct={bili_jct}",
    }
    all_ups = []
    pn = 1
    async with aiohttp.ClientSession() as session:
        while True:
            async with session.get(
                "https://api.bilibili.com/x/relation/followings",
                params={"vmid": uid, "pn": pn, "ps": 50, "order": "desc"},
                headers=headers, timeout=15
            ) as r:
                data = await r.json(content_type=None)
            if data.get("code") != 0:
                break
            items = (data.get("data") or {}).get("list") or []
            if not items:
                break
            all_ups.extend(items)
            if len(items) < 50:
                break
            pn += 1
    FOLLOWINGS_CACHE.write_text(json.dumps({"ups": all_ups, "total": len(all_ups)}))
    return all_ups


# ─── 筛活跃 UP 主 ──────────────────────────────────

async def find_active(ups: List[Dict], days: int = 30, progress_file: str = "/tmp/active_progress.log",
                   cache_path: str = None, cache_every: int = 100) -> List[Dict]:
    """对每个 UP 拉最新视频, 过滤出 N 天内有更新的, 实时写 cache"""
    from bilibili_api import user as bili_user
    cutoff_ts = int((datetime.now() - timedelta(days=days)).timestamp())
    active = []
    total = len(ups)
    # 用 credential 减少风控
    cd = load_credential()
    cred = None
    try:
        from bilibili_api import Credential
        cred = Credential(
            sessdata=cd.get("sessdata", ""),
            bili_jct=cd.get("bili_jct", ""),
            buvid3=cd.get("buvid3", ""),
            dedeuserid=cd.get("dedeuserid", ""),
        )
    except Exception:
        pass
    # 写进度
    with open(progress_file, "w") as f:
        f.write(f"开始扫描 {total} 个 UP, cutoff {datetime.fromtimestamp(cutoff_ts)}\n")
    for i, up in enumerate(ups, 1):
        mid = str(up.get("mid"))
        try:
            u = bili_user.User(uid=int(mid), credential=cred) if cred else bili_user.User(uid=int(mid))
            v = await u.get_videos()
            vlist = (v or {}).get("list", {}).get("vlist", []) or []
            if vlist:
                latest_ts = vlist[0].get("created", 0)
                if latest_ts > cutoff_ts:
                    recent = []
                    for v_item in vlist[:5]:
                        ts = v_item.get("created", 0)
                        recent.append({
                            "bvid": v_item.get("bvid"),
                            "title": v_item.get("title", "?"),
                            "created_ts": ts,
                            "created_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?",
                        })
                    active.append({
                        "mid": mid,
                        "name": up.get("uname", "?"),
                        "sign": up.get("sign", ""),
                        "recent_videos": recent,
                    })
        except Exception as e:
            pass
        # 限频
        await asyncio.sleep(0.3)
        # 进度
        if i % 50 == 0 or i == total:
            with open(progress_file, "a") as f:
                f.write(f"  [{i}/{total}] 活跃: {len(active)}\n")
        # 实时 cache
        if cache_path and (i % cache_every == 0 or i == total):
            try:
                Path(cache_path).write_text(json.dumps(active, ensure_ascii=False))
            except Exception:
                pass
    return active


# ─── 主题提取 (LLM, 小批) ──────────────────────────

THEME_PROMPT = '''从这些视频标题中提取 3-5 个核心主题词。
每个主题 1-4 字中文, 用于聚类展示。

只输出 JSON 数组, 无 markdown: ["主题1", "主题2", "主题3"]

视频标题:
{titles}
'''


def extract_themes(client_cfg, titles: List[str]) -> List[str]:
    """LLM 提取主题"""
    if not titles:
        return []
    prompt = THEME_PROMPT.format(titles="\n".join(f"- {t}" for t in titles[:8]))
    try:
        result = _llm_call(
            client_cfg,
            system="只输出 JSON 数组, 无解释。",
            user=prompt, max_tokens=200,
        )
        # 解析
        import re
        m = re.search(r"\[.*?\]", result, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return [str(t)[:15] for t in data[:5]]
    except Exception:
        pass
    return []


# ─── 主流程 ──────────────────────────────────

async def main_async():
    print("=" * 60, flush=True)
    print("🚀 活跃 UP 主 + 视频主题图谱", flush=True)
    print("=" * 60, flush=True)

    cd = load_credential()
    uid = int(cd.get("dedeuserid", 0))
    sessdata = cd.get("sessdata", "")
    bili_jct = cd.get("bili_jct", "")

    # 1. 拉关注
    print("📂 拉关注列表...", flush=True)
    all_ups = await fetch_all_followings(uid, sessdata, bili_jct)
    print(f"   总关注: {len(all_ups)}", flush=True)

    # 2. 筛活跃
    if ACTIVE_CACHE.exists():
        age_hours = (datetime.now().timestamp() - ACTIVE_CACHE.stat().st_mtime) / 3600
        if age_hours < 12:
            print(f"📂 用活跃缓存 (年龄 {age_hours:.1f}h)", flush=True)
            active = json.loads(ACTIVE_CACHE.read_text())
        else:
            print(f"📂 缓存过期, 重新扫描 (实时 cache, 15-25 分钟)...", flush=True)
            active = await find_active(all_ups, days=30,
                                       cache_path=str(ACTIVE_CACHE),
                                       cache_every=100)
    else:
        print("📂 扫描活跃 UP 主 (实时 cache, 15-25 分钟)...", flush=True)
        active = await find_active(all_ups, days=30,
                                   cache_path=str(ACTIVE_CACHE),
                                   cache_every=100)
    print(f"✅ 活跃 UP 主: {len(active)}", flush=True)

    # 3. 关键词粗分
    print("🔍 关键词粗分...", flush=True)
    for up in active:
        r = keyword_fallback(up["name"], up.get("sign", ""), [], DEFAULT_CATEGORIES)
        up["category"] = r["category"]
        up["confidence"] = r["confidence"]
        up["topics"] = r.get("topics", [])
    by_cat = Counter(u["category"] for u in active)
    print(f"   分类分布: {dict(by_cat.most_common())}", flush=True)

    # 4. LLM 主题提取 (限 50 个避免太慢)
    try:
        cfg = _get_llm_client()
        print(f"🤖 LLM: {cfg['model']}", flush=True)
    except Exception as e:
        print(f"❌ {e}", flush=True)
        return
    LLM_N = 50
    print(f"⏳ LLM 主题提取 (限 {LLM_N} 个, 5-10 分钟)...", flush=True)
    for i, up in enumerate(active[:LLM_N], 1):
        titles = [v["title"] for v in up.get("recent_videos", [])]
        themes = extract_themes(cfg, titles)
        up["video_themes"] = themes
        # 同时 LLM 精分 (覆盖关键词)
        recent = [f"{v['created_str']} | {v['title']}" for v in up.get("recent_videos", [])[:5]]
        classify = classify_up(cfg, "综合 UP 主类型", {
            **up,
            "summary": " ".join(titles),
            "transcript_len": sum(len(t) for t in titles),
        })
        if classify.get("category") in DEFAULT_CATEGORIES:
            up["category"] = classify["category"]
            up["confidence"] = classify["confidence"]
            up["topics"] = classify.get("topics", [])
        await asyncio.sleep(1.5)
        if i % 10 == 0:
            print(f"    {i}/{LLM_N}", flush=True)
    # 没 LLM 的 UP 主保持关键词结果
    for up in active[LLM_N:]:
        up["video_themes"] = []

    # 5. 报告
    print("📝 生成报告...", flush=True)
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 活跃 UP 主 + 视频主题图谱 - {today}\n",
        f"## 📊 总览\n",
        f"- **总关注**: {len(all_ups)}",
        f"- **月活 UP 主**: {len(active)}",
        f"- **LLM 精分**: {min(LLM_N, len(active))} 个",
        f"- **时间窗**: 过去 30 天\n",
        f"## 📂 分类分布\n",
    ]
    for cat in DEFAULT_CATEGORIES + ["其他"]:
        n = by_cat.get(cat, 0)
        if n > 0:
            pct = n / max(1, len(active)) * 100
            lines.append(f"- **{cat}**: {n} 个 ({pct:.1f}%)")

    # 详细列表
    for cat in DEFAULT_CATEGORIES + ["其他"]:
        cat_ups = [u for u in active if u["category"] == cat]
        if not cat_ups:
            continue
        lines.append(f"\n## 📂 {cat} ({len(cat_ups)} 个)\n")
        for up in sorted(cat_ups, key=lambda x: -len(x.get("recent_videos", [])))[:30]:
            themes = up.get("video_themes", [])
            topics = up.get("topics", [])
            themes_str = " ".join(f"`{t}`" for t in themes) or "—"
            topics_str = " ".join(f"`{t}`" for t in topics) or "—"
            recent = up.get("recent_videos", [])
            latest = recent[0]["title"] if recent else "—"
            lines.append(f"- **{up['name']}** (UID: {up['mid']})")
            lines.append(f"  - 分类置信度: {up.get('confidence', '?')}")
            lines.append(f"  - UP 主话题: {topics_str}")
            lines.append(f"  - 视频主题: {themes_str}")
            lines.append(f"  - 最新: {latest}")
        if len(cat_ups) > 30:
            lines.append(f"- ... 及其他 {len(cat_ups) - 30} 个")

    lines.append("\n---\n*Generated by up_classify_active v1.0*")
    report = "\n".join(lines)

    out_md = OUTPUT_DIR / "up_classify_active.md"
    out_md.write_text(report, encoding="utf-8")
    print(f"   报告: {out_md}", flush=True)

    # 6. 大图
    print("📊 画大图谱...", flush=True)
    out_png = OUTPUT_DIR / "up_classify_active_graph.png"
    await asyncio.to_thread(draw_active_graph, active, out_png)
    print(f"   图: {out_png}", flush=True)

    # 7. 主题图 (UP主-主题)
    out_themes = OUTPUT_DIR / "up_themes.json"
    themes_data = []
    for up in active:
        themes_data.append({
            "name": up["name"],
            "mid": up["mid"],
            "category": up["category"],
            "themes": up.get("video_themes", []),
            "topics": up.get("topics", []),
        })
    out_themes.write_text(json.dumps(themes_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   主题 JSON: {out_themes}", flush=True)

    return report


def draw_active_graph(active: List[Dict], output: Path):
    """画 UP 主 - 分类 - 主题 大图"""
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
        print("⚠️ 需要 networkx matplotlib", flush=True)
        return

    G = nx.Graph()
    cat_nodes = set()
    up_nodes = set()
    theme_nodes = set()
    topic_nodes = set()
    edges = []

    for up in active:
        up_id = f"UP:{up['name']}"
        up_nodes.add(up_id)
        cat_id = f"类:{up['category']}"
        cat_nodes.add(cat_id)
        edges.append((up_id, cat_id, 2.0))
        # 视频主题
        for t in up.get("video_themes", []):
            t_id = f"视频主题:{t}"
            theme_nodes.add(t_id)
            edges.append((up_id, t_id, 0.5))
        # UP 主话题
        for t in up.get("topics", []):
            t_id = f"UP话题:{t}"
            topic_nodes.add(t_id)
            edges.append((up_id, t_id, 0.7))

    for n in up_nodes:
        G.add_node(n, type="up")
    for n in cat_nodes:
        G.add_node(n, type="cat")
    for n in theme_nodes:
        G.add_node(n, type="theme")
    for n in topic_nodes:
        G.add_node(n, type="topic")
    for a, b, w in edges:
        G.add_edge(a, b, weight=w)

    fig, ax = plt.subplots(figsize=(28, 20))
    pos = nx.spring_layout(G, k=1.5, seed=42, weight="weight")
    color_map = []
    size_map = []
    for n in G.nodes:
        if n.startswith("UP:"):
            color_map.append("#4A90E2")
            size_map.append(150)
        elif n.startswith("类:"):
            color_map.append("#F5A623")
            size_map.append(400)
        elif n.startswith("视频主题:"):
            color_map.append("#7ED321")
            size_map.append(80)
        else:  # UP话题
            color_map.append("#BD10E0")
            size_map.append(120)
    nx.draw(G, pos, node_color=color_map, node_size=size_map,
            with_labels=True, font_size=5, font_weight="bold",
            edge_color="#bbb", alpha=0.85, ax=ax)
    from matplotlib.lines import Line2D
    legend = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#4A90E2", markersize=10, label="UP 主"),
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#F5A623", markersize=10, label="分类"),
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#7ED321", markersize=10, label="视频主题"),
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#BD10E0", markersize=10, label="UP 话题"),
    ]
    ax.legend(handles=legend, loc="upper right")
    ax.set_title(f"活跃 UP 主 + 视频主题图谱 ({len(active)} 个 UP) - {datetime.now().strftime('%Y-%m-%d')}", fontsize=14)
    plt.tight_layout()
    plt.savefig(output, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"   大图: {output}", flush=True)


if __name__ == "__main__":
    asyncio.run(main_async())
