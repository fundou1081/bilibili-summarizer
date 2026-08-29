#!/usr/bin/env python3
"""
B 站 UP 主完整分类 (大数量级优化版)

策略:
  1. 关键词粗分 2500+ UP 主 (毫秒级)
  2. 找出"模糊"的 (无匹配/多分类 match) - top N + 重要样本
  3. LLM 精细分类这些 (50-100 个)
  4. 输出综合报告 + 知识图谱
"""

import sys
import os
import json
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from summarize import _llm_call, _get_llm_client
from bilibili_cc import load_credential

# 复用 up_classifier 的关键词 + 分类函数
from up_classifier import (
    DEFAULT_CATEGORIES, keyword_fallback, classify_up,
    CLASSIFY_PROMPT, build_prompt, parse_classify_json,
)

# 路径
OUTPUT_DIR = Path.home() / "my_bili_data"
FOLLOWINGS_CACHE = OUTPUT_DIR / "followings_cache.json"

# ─── 拉取所有关注 ──────────────────────────────────────

async def fetch_all_followings(uid: int, sessdata: str, bili_jct: str) -> List[Dict]:
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
            try:
                async with session.get(
                    "https://api.bilibili.com/x/relation/followings",
                    params={"vmid": uid, "pn": pn, "ps": 50, "order": "desc"},
                    headers=headers, timeout=15
                ) as r:
                    data = await r.json(content_type=None)
            except Exception as e:
                print(f"  ⚠️ page {pn}: {e}", file=sys.stderr)
                break
            if data.get("code") != 0:
                print(f"  ❌ page {pn} code={data.get('code')}", file=sys.stderr)
                break
            items = (data.get("data") or {}).get("list") or []
            if not items:
                break
            all_ups.extend(items)
            print(f"  第 {pn} 页: +{len(items)} (累计 {len(all_ups)})", file=sys.stderr)
            if len(items) < 50:
                break
            pn += 1
            if pn > 100:  # 安全阀
                break
    return all_ups


# ─── 关键词粗分 (整批处理) ──────────────────────────

def keyword_batch(ups: List[Dict], categories: List[str]) -> Dict[str, Dict]:
    """对所有 UP 主做关键词粗分, 返回 {mid: {category, confidence, topics, reason}}"""
    results = {}
    for u in ups:
        mid = str(u.get("mid", ""))
        name = u.get("uname", "?")
        sign = u.get("sign", "")
        text = f"{name} {sign}".lower()
        # 复用 up_classifier 的 keyword_fallback
        r = keyword_fallback(name, sign, [], categories)
        r["name"] = name
        r["sign"] = sign
        r["mtime"] = u.get("mtime", 0)  # 关注时间
        results[mid] = r
    return results


# ─── 找出需要 LLM 的 ────────────────────────────────

def select_for_llm(coarse: Dict[str, Dict], n_max: int = 50) -> List[str]:
    """选择需要 LLM 精细分类的 UP 主

    优先级:
      1. 关键词无匹配 (category=其他)
      2. 置信度 low 且 sign 有内容 (可能是非典型)
      3. 重要的 (用户指定的 - hardcoded)
    """
    # 1. 必选: 关键词 fallback 到"其他"的
    need_llm = []
    seen = set()
    for mid, r in coarse.items():
        if r["category"] == "其他":
            need_llm.append(mid)
            seen.add(mid)
    # 2. 选: 置信度 low 但有签名 (可能关键词规则不全)
    for mid, r in coarse.items():
        if mid in seen:
            continue
        if r.get("confidence") == "low" and r.get("sign"):
            need_llm.append(mid)
            seen.add(mid)
            if len(need_llm) >= n_max:
                break
    # 3. 必选: 用户指定的 3 个
    must_have = ["3546659109735216", "523635048", "482374377"]
    for mid in must_have:
        if mid in coarse and mid not in seen:
            need_llm.insert(0, mid)
            seen.add(mid)
    return need_llm[:n_max]


# ─── LLM 批量分类 ──────────────────────────────────

async def llm_classify_batch(client_cfg, ups_info: Dict[str, Dict], mids: List[str], categories: List[str]) -> Dict[str, Dict]:
    """对 mid 列表做 LLM 分类, 串行避免限频"""
    from bilibili_api import user as bili_user
    results = {}
    for i, mid in enumerate(mids, 1):
        info = ups_info.get(mid, {})
        uid = mid
        name = info.get("name", "?")
        sign = info.get("sign", "")
        # 拉 UP 主最近视频
        try:
            u = bili_user.User(uid=int(uid))
            vresult = await u.get_videos()
            vlist = (vresult or {}).get("list", {}).get("vlist", []) or []
            recent = []
            for v in vlist[:5]:
                ts = v.get("created", 0)
                recent.append({
                    "title": v.get("title", "?"),
                    "created_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?",
                })
        except Exception as e:
            recent = []
        # LLM 评估
        recent_titles = [f"{v['created_str'][:10]} | {v['title'][:60]}" for v in recent]
        if not recent_titles:
            recent_titles = ["(无最近视频)"]
        prompts = []
        for attempt in range(2):
            try:
                prompt = build_prompt(uid, name, sign, "?", 0, 0, recent_titles, categories)
                result = _llm_call(
                    client_cfg,
                    system="你是 B 站 UP 主内容分类助手。只输出 JSON。",
                    user=prompt, max_tokens=300,
                )
                parsed = parse_classify_json(result)
                if parsed and parsed.get("category") in categories:
                    results[mid] = parsed
                    results[mid]["_method"] = "llm"
                    break
            except Exception:
                continue
        if mid not in results:
            # fallback 到关键词结果
            results[mid] = info  # 之前关键词的结果
            results[mid]["_method"] = "fallback"
        # 限频
        await asyncio.sleep(1.0)
        # 进度
        if i % 5 == 0 or i == len(mids):
            print(f"    LLM {i}/{len(mids)}", file=sys.stderr, flush=True)
    return results


# ─── 合并 + 报告 ──────────────────────────────────

def merge_and_report(coarse: Dict, llm: Dict, categories: List[str], total: int) -> str:
    """合并粗分+LLM, 生成报告"""
    today = datetime.now().strftime("%Y-%m-%d")
    # 合并
    final = {}
    for mid, r in coarse.items():
        if mid in llm:
            final[mid] = {**r, **llm[mid], "name": r.get("name"), "sign": r.get("sign")}
        else:
            final[mid] = r
    # 统计
    by_cat = defaultdict(list)
    for mid, r in final.items():
        by_cat[r["category"]].append(r)

    lines = [
        f"# 完整 UP 主分类报告 - {today}\n",
        f"## 📊 总览\n",
        f"- **关注 UP 主总数**: {total}",
        f"- **实际分类数**: {len(final)}",
        f"- **LLM 精细分类**: {len(llm)} 个",
        f"- **关键词粗分**: {len(final) - len(llm)} 个\n",
        f"## 📂 分类分布\n",
    ]
    for cat in categories:
        n = len(by_cat.get(cat, []))
        if n > 0:
            pct = n / max(1, len(final)) * 100
            lines.append(f"- **{cat}**: {n} 个 ({pct:.1f}%)")
    lines.append(f"- **其他**: {len(by_cat.get('其他', []))} 个")
    lines.append("")

    # 各类详情
    for cat in categories + ["其他"]:
        if cat not in by_cat or not by_cat[cat]:
            continue
        ups = sorted(by_cat[cat], key=lambda x: -x.get("mtime", 0))
        lines.append(f"\n## 📂 {cat} ({len(ups)} 个)\n")
        # 显示前 20 个, 按关注时间倒序
        for r in ups[:20]:
            mid = r.get("mid", "?")
            name = r.get("name", "?")
            sign = (r.get("sign", "") or "")[:50]
            conf = r.get("confidence", "?")
            topics = r.get("topics", [])
            method = r.get("_method", "fallback")
            topics_str = " ".join(f"`{t}`" for t in topics[:3]) or "—"
            method_icon = "🧠" if method == "llm" else "🔍"
            lines.append(f"- {method_icon} **{name}** (UID: {mid})")
            if sign:
                lines.append(f"  - 签名: {sign}")
            lines.append(f"  - 置信度: {conf} | 话题: {topics_str}")
        if len(ups) > 20:
            lines.append(f"- ... 及其他 {len(ups) - 20} 个")

    # 知识图谱数据 (PNG 单独画)
    lines.append("\n## 🛠️ 执行\n")
    lines.append(f"- LLM 分类置信度更高 (`🧠`), 关键词 fallback 较粗 (`🔍`)\n")
    lines.append("---\n")
    lines.append("*Generated by up_classify_full v1.0*")
    return "\n".join(lines), final


# ─── 知识图谱 ──────────────────────────────────

def draw_full_graph(final: Dict, output: Path, max_nodes: int = 200):
    """画完整图谱 (限制节点数避免太密)"""
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
        print("⚠️ 需要 networkx matplotlib", file=sys.stderr)
        return

    # 限制: 每个分类最多 15 个 UP 主 (取 confidence 高的 + 最近的)
    by_cat = defaultdict(list)
    for mid, r in final.items():
        score = (3 if r.get("_method") == "llm" else 1) if r.get("confidence") == "high" else 1
        if r.get("confidence") == "medium":
            score = 2
        by_cat[r["category"]].append((score, -r.get("mtime", 0), mid, r))

    G = nx.Graph()
    for cat, items in by_cat.items():
        items.sort(key=lambda x: (-x[0], x[1]))
        for score, _, mid, r in items[:15]:
            G.add_node(f"UP:{r.get('name', '?')}", type="up", cat=cat)
            G.add_node(f"类:{cat}", type="cat")
            G.add_edge(f"UP:{r.get('name', '?')}", f"类:{cat}", weight=1.0)
            for t in r.get("topics", [])[:2]:
                G.add_node(f"话题:{t}", type="topic")
                G.add_edge(f"UP:{r.get('name', '?')}", f"话题:{t}", weight=0.5)

    if len(G) == 0:
        print("⚠️ 无数据画图", file=sys.stderr)
        return

    # 画
    fig, ax = plt.subplots(figsize=(20, 14))
    pos = nx.spring_layout(G, k=1.2, seed=42)
    color_map = []
    size_map = []
    for n in G.nodes:
        if n.startswith("UP:"):
            color_map.append("#4A90E2")
            size_map.append(200)
        elif n.startswith("类:"):
            color_map.append("#F5A623")
            size_map.append(500)
        else:
            color_map.append("#7ED321")
            size_map.append(80)
    nx.draw(G, pos, node_color=color_map, node_size=size_map,
            with_labels=True, font_size=6, font_weight="bold",
            edge_color="#999", alpha=0.85, ax=ax)
    from matplotlib.lines import Line2D
    legend = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#4A90E2", markersize=10, label="UP 主"),
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#F5A623", markersize=10, label="分类"),
        Line2D([0], [0], marker='o', color='w', markerfacecolor="#7ED321", markersize=10, label="话题"),
    ]
    ax.legend(handles=legend, loc="upper right")
    ax.set_title(f"UP 主完整分类图谱 - {datetime.now().strftime('%Y-%m-%d')}", fontsize=14)
    plt.tight_layout()
    plt.savefig(output, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"📊 图谱: {output}", file=sys.stderr)


# ─── 主流程 ──────────────────────────────────

async def main_async():
    print("=" * 60)
    print("🚀 B 站 UP 主完整分类")
    print("=" * 60)

    # 1. 拉关注
    cd = load_credential()
    uid = int(cd.get("dedeuserid", 0))
    sessdata = cd.get("sessdata", "")
    bili_jct = cd.get("bili_jct", "")
    if not uid or not sessdata:
        print("❌ 未登录")
        return

    if FOLLOWINGS_CACHE.exists():
        cache_mtime = FOLLOWINGS_CACHE.stat().st_mtime
        age_hours = (datetime.now().timestamp() - cache_mtime) / 3600
        if age_hours < 24:
            print(f"📂 用缓存 (年龄 {age_hours:.1f}h): {FOLLOWINGS_CACHE}")
            data = json.loads(FOLLOWINGS_CACHE.read_text())
            ups = data["ups"]
            total = data["total"]
        else:
            print(f"📂 缓存过期 ({age_hours:.1f}h), 重新拉")
            ups = await fetch_all_followings(uid, sessdata, bili_jct)
            total = len(ups)
            FOLLOWINGS_CACHE.write_text(json.dumps({"ups": ups, "total": total, "ts": datetime.now().isoformat()}, ensure_ascii=False))
    else:
        print("📂 拉关注列表...")
        ups = await fetch_all_followings(uid, sessdata, bili_jct)
        total = len(ups)
        FOLLOWINGS_CACHE.write_text(json.dumps({"ups": ups, "total": total, "ts": datetime.now().isoformat()}, ensure_ascii=False))
        print(f"   已缓存到 {FOLLOWINGS_CACHE}")

    print(f"📊 关注总数: {total}")

    # 2. 关键词粗分
    print("🔍 关键词粗分...")
    coarse = keyword_batch(ups, DEFAULT_CATEGORIES)
    n_need = sum(1 for r in coarse.values() if r["category"] == "其他")
    n_low = sum(1 for r in coarse.values() if r.get("confidence") == "low" and r.get("sign"))
    print(f"   粗分结果: 其他={n_need}, low={n_low}, 已分类={len(coarse) - n_need - n_low}")

    # 3. 选 LLM 目标
    LLM_N = 30  # 限 30 个, 2-3 分钟
    mids_for_llm = select_for_llm(coarse, n_max=LLM_N)
    print(f"🧠 选 {len(mids_for_llm)} 个 LLM 精分 (限 {LLM_N})", flush=True)

    # 4. LLM 分类
    try:
        cfg = _get_llm_client()
        print(f"🤖 LLM: {cfg['model']}", flush=True)
    except Exception as e:
        print(f"❌ {e}", flush=True)
        return
    print("⏳ LLM 分类 (限频 1s/个)...", flush=True)
    ups_info = {str(u.get("mid")): {"name": u.get("uname"), "sign": u.get("sign", "")} for u in ups}
    llm_results = await llm_classify_batch(cfg, ups_info, mids_for_llm, DEFAULT_CATEGORIES)

    # 5. 合并 + 报告
    print("📝 生成报告...")
    report, final = merge_and_report(coarse, llm_results, DEFAULT_CATEGORIES, total)
    output_md = OUTPUT_DIR / "up_classify_full.md"
    output_md.write_text(report, encoding="utf-8")
    print(f"   报告: {output_md}")

    # 6. 图谱
    print("📊 画图谱...")
    output_png = OUTPUT_DIR / "up_classify_full_graph.png"
    draw_full_graph(final, output_png)

    # 7. 发飞书
    print("📤 发飞书...")
    # 简化发: 文件路径 + 简短总结
    return report


if __name__ == "__main__":
    asyncio.run(main_async())
