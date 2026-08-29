#!/usr/bin/env python3
"""主题图 v2 - 字幕实时 cache + 改进关键词"""

import sys, os, json, asyncio, re
from pathlib import Path
from datetime import datetime
from collections import Counter, defaultdict

sys.path.insert(0, str(Path(__file__).parent))
from theme_graph import sample_ups, fetch_video_subtitle, draw_theme_graph
from bilibili_api import Credential
from bilibili_cc import load_credential

OUTPUT_DIR = Path.home() / "my_bili_data"

# 停用词 (扩充, 含指代词/常用词)
STOP_WORDS = set('''
了 是 就在 和 也 都 有 很 这个 那个 这样 那样 一些 怎么 但是 但是 的 我 不 你 他 她 它们 等 与 及 或 被 因为 所以 如果 这时 这些 那些 所有 以及 或者 已经 就是 可以 可能 应该 需要 想要 知道 大家 我们 你们 自己 其实 然后 比如 那种 某些 这个 那个 这些 那些 那么 哪儿 哪里 怎么 怎么 怎 为什么 怎样 怎么 几 多 少 是不是 只能 不是 没有 没 看 说 听 来 去 做 想 让 给 打 找 走 跟 拿 放 起 上 下 里 外 前 后 左 右 中 间 之 来 一 个 两种 三种 一些 一种 一种 一个 两个 三个 多个
什么 哪 哪里 谁 怎么 怎么样 为什么 这样 那样 这样 这样 那种 某 某些 此 这那
然后 就是 就是 这是 这是 那是 这是 这是 那种 那种 一些一些 一个一个
我们 你 他们 它们 自己 本人 某 某某
一些 一件 一块 一杯 一份 一片 一部 一张 一个 一条 一项
'''.split())
STOP_WORDS.update(['哦', '啊', '嗯', '哈', '呀', '嘛', '吧', '呢', '吗', '啦', '哎', '欸', '唉', '哈喽', '啦'])


def extract_themes_v2(text: str, top_k: int = 5) -> list:
    """改进关键词提取: 跳过停用词/数字/单字"""
    if not text:
        return []
    # 保留中文
    text = re.sub(r'[^\u4e00-\u9fff]', '', text)
    text = re.sub(r'\s+', '', text).strip()
    # 提取 2-4 字词
    counter = Counter()
    for n in [2, 3, 4]:
        for i in range(len(text) - n + 1):
            word = text[i:i+n]
            if word in STOP_WORDS:
                continue
            # 跳过包含数字
            if any(c in '0123456789' for c in word):
                continue
            counter[word] += 1
    return [w for w, c in counter.most_common(top_k)]


async def main_async():
    print("=" * 60)
    print("主题图 v2 - 字幕 cache + 改进关键词")
    print("=" * 60)

    active = json.loads((OUTPUT_DIR / "active_ups_classified.json").read_text())
    sampled = sample_ups(active)
    print(f"采样 {len(sampled)} 个 UP")

    tasks = []
    for up in sampled:
        for v in up.get("recent_videos", [])[:2]:
            tasks.append({
                "up_name": up["name"],
                "up_mid": up["mid"],
                "category": up.get("category", "其他"),
                "bvid": v["bvid"],
                "title": v["title"],
                "created_str": v.get("created_str", "?"),
            })
    print(f"视频: {len(tasks)}")

    # 字幕 cache
    sub_cache_file = OUTPUT_DIR / "theme_subtitles_cache.json"
    sub_cache = {}
    if sub_cache_file.exists():
        try:
            sub_cache = json.loads(sub_cache_file.read_text())
            print(f"用字幕 cache ({len(sub_cache)} 条)")
        except Exception:
            pass

    cred = Credential(
        sessdata=load_credential().get("sessdata", ""),
        bili_jct=load_credential().get("bili_jct", ""),
        buvid3=load_credential().get("buvid3", "") or "",
        dedeuserid=load_credential().get("dedeuserid", ""),
    )

    items = []
    for i, t in enumerate(tasks, 1):
        bvid = t["bvid"]
        if bvid in sub_cache:
            t["transcript"] = sub_cache[bvid]
            t["transcript_len"] = len(sub_cache[bvid])
        else:
            info = await fetch_video_subtitle(bvid, cred=cred)
            if info and info.get("transcript"):
                t["transcript"] = info["transcript"]
                t["transcript_len"] = info["transcript_len"]
                t["duration"] = info.get("duration", 0)
                sub_cache[bvid] = t["transcript"]
            else:
                t["transcript"] = ""
                t["transcript_len"] = 0
            # 每 30 个写一次 cache
            if i % 30 == 0:
                sub_cache_file.write_text(json.dumps(sub_cache), encoding="utf-8")
                print(f"  [{i}/{len(tasks)}] cache saved", flush=True)
        items.append(t)
        await asyncio.sleep(0.4)

    # 最后写
    sub_cache_file.write_text(json.dumps(sub_cache), encoding="utf-8")

    n_with = sum(1 for x in items if x.get("transcript_len", 0) > 0)
    print(f"\n✅ 有字幕: {n_with}/{len(items)} ({n_with/max(1,len(items))*100:.0f}%)")

    # 主题提取: 用字幕 (有的话), 否则用标题
    for item in items:
        text = item.get("transcript", "")
        if len(text) > 50:
            themes = extract_themes_v2(text, top_k=5)
            item["themes"] = themes
            # 1 句话总结: 字幕前 150 字
            item["summary"] = text[:150].replace("\n", " ") + "..."
        else:
            themes = extract_themes_v2(item["title"], top_k=5)
            item["themes"] = themes
            item["summary"] = item["title"]

    # 统计
    theme_counter = Counter()
    for item in items:
        for t in item.get("themes", []):
            theme_counter[t] += 1

    # 数据
    data = {
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
                "has_subtitle": i.get("transcript_len", 0) > 50,
            }
            for i in items
        ],
    }
    out = OUTPUT_DIR / "video_summaries.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"数据: {out}")

    # 报告
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 主题图报告 - {today}",
        "",
        "## 📊 总览",
        f"- **采样 UP 主**: {len(sampled)}",
        f"- **处理视频**: {len(items)}",
        f"- **有字幕**: {n_with}",
        f"- **独立主题**: {len(theme_counter)}",
        f"- **方法**: 字幕 2-4 字关键词 (有字幕) / 标题关键词 (无字幕)",
        "",
        "## 🔥 Top 30 主题",
        "",
    ]
    for t, c in theme_counter.most_common(30):
        lines.append(f"- **{t}**: {c}")

    lines.extend(["", "## 📂 主题 - 视频映射 (Top 30)", ""])
    for theme, _ in theme_counter.most_common(30):
        vids = [i for i in items if theme in i.get("themes", [])]
        if not vids:
            continue
        lines.append(f"### {theme} ({len(vids)})")
        lines.append("")
        for v in vids[:5]:
            lines.append(f"- **{v['up_name']}**: {v['title'][:50]}")
            if v.get("summary") and v.get("has_subtitle"):
                lines.append(f"  - {v['summary'][:80]}")
        if len(vids) > 5:
            lines.append(f"- ...及其他 {len(vids) - 5}")
        lines.append("")

    lines.append("---\n*Generated by theme_graph_v2*")

    out_md = OUTPUT_DIR / "theme_report.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告: {out_md}")

    # 图
    import matplotlib
    matplotlib.use("Agg")
    out_png = OUTPUT_DIR / "theme_graph.png"
    draw_theme_graph(data, out_png)
    print(f"图: {out_png}")


if __name__ == "__main__":
    asyncio.run(main_async())