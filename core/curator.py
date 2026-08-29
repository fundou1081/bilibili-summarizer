#!/usr/bin/env python3
"""
B 站 AI 收藏夹助手 v2.0 (Python 端, 零默认 API 写操作)

用法:
  # 从本地 video 库评估 (默认)
  python3 bilibili_curator.py

  # 从 B 站收藏夹评估 (需要登录)
  python3 bilibili_curator.py --source favorites

  # 评估稍后观看 / 关注列表 / 全部
  python3 bilibili_curator.py --source watch_later
  python3 bilibili_curator.py --source followings
  python3 bilibili_curator.py --source all

  # 自定义标准
  python3 bilibili_curator.py --standard "AI/芯片/半导体相关"

  # 默认 dry-run (不调 B 站 API 写操作)
  # 要执行 (创建收藏夹 + 移入视频), 显式加 --execute
  python3 bilibili_curator.py --source favorites --execute

  # 输出文件
  python3 bilibili_curator.py -o curator-2026-06-27.md

  # dry-run (不调 LLM, 只统计)
  python3 bilibili_curator.py --dry-run
"""

import sys
import os
import json
import argparse
import re
import time
import asyncio
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent))
from summarize import _llm_call, _get_llm_client

try:
    from bilibili_cc import load_credential, has_credential
    from bilibili_api import Credential
    BILIBILI_API_OK = True
except ImportError:
    BILIBILI_API_OK = False

DEFAULT_DOWNLOADS = Path.home() / "my_bili_data" / "downloads"


# ─── LLM Prompts ───────────────────────────────────────────

DEFAULT_PROMPT = '''你是 B 站视频价值判断助手。给定一个视频, 判断是否值得用户收藏。

用户偏好: {standard}

从 4 个维度各给 0-3 分 (总分 0-12):
1. content (内容质量): 深度/原创/信息密度
2. fresh (时效性): 不会很快过时的程度
3. useful (实用性): 用户能反复参考
4. match (匹配度): 与用户偏好的契合度

只输出这一行 JSON (无 markdown, 无解释, 无换行):
{{"content":N,"fresh":N,"useful":N,"match":N,"total":N,"verdict":"keep|maybe|drop","reason":"<20字","topics":["a","b"]}}

verdict 规则:
- total >= 9: "keep"
- total 5-8: "maybe"
- total < 5: "drop"

视频标题: {title}
UP 主: {author}
发布日期: {date}
时长: {duration}
摘要: {summary}
字幕长度: {transcript_len} 字
'''

SIMPLE_PROMPT = '''判断这个 B 站视频是否值得收藏。
用户偏好: {standard}
标题: {title}
UP: {author}

只输出 JSON (一行, 无 markdown): {{"score": <0-10>, "tier": "high|medium|skip", "reason": "<15字"}}
- score >= 7: high
- 4-6: medium
- < 4: skip
无摘要时 score ≤ 5, reason 含 "无内容验证"。
'''


def build_prompt(standard: str, v: Dict) -> str:
    summary = (v.get("summary") or "").strip()
    if not summary:
        summary = "(无字幕/摘要, 仅看标题和 UP 主)"
    elif len(summary) > 1500:
        summary = summary[:1500] + "..."
    note = ""
    if not (v.get("summary") or "").strip():
        note = "\n无摘要时各维度都 ≤ 1, reason 写 '无内容验证'。"
    return DEFAULT_PROMPT.format(
        standard=standard,
        title=v.get("title", "?"),
        author=v.get("author", "?"),
        date=v.get("created_str", "?"),
        duration=v.get("duration_str", "?"),
        summary=summary,
        transcript_len=v.get("transcript_len", 0),
    ) + note


def build_simple_prompt(standard: str, v: Dict) -> str:
    return SIMPLE_PROMPT.format(
        standard=standard,
        title=v.get("title", "?"),
        author=v.get("author", "?"),
    )


def parse_llm_json(text: str) -> Optional[Dict]:
    if not text or not text.strip():
        return None
    text = text.strip()
    if "```" in text:
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

    score = data.get("score")
    if score is None:
        score = data.get("total")
    if score is None:
        return None
    try:
        score = int(score)
        if score < 0:
            score = 0
        elif score > 12:
            score = 12
    except (TypeError, ValueError):
        return None

    tier = data.get("tier") or data.get("verdict")
    tier_map = {"keep": "high", "maybe": "medium", "drop": "skip"}
    tier = tier_map.get(tier, tier)
    if tier not in ("high", "medium", "skip"):
        if score >= 7:
            tier = "high"
        elif score >= 4:
            tier = "medium"
        else:
            tier = "skip"

    reason = str(data.get("reason", ""))[:80] or "(无理由)"
    topics = data.get("topics") or data.get("key_topics") or []
    if not isinstance(topics, list):
        topics = []
    topics = [str(t)[:20] for t in topics[:5]]

    return {"score": score, "tier": tier, "reason": reason, "topics": topics}


# ─── 数据源: local ─────────────────────────────────────────

def load_local(downloads_dir: Path) -> List[Dict]:
    if not downloads_dir.exists():
        return []
    videos = []
    for d in sorted(downloads_dir.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        summary = ""
        sp = d / "summary.md"
        if sp.exists():
            try:
                summary = sp.read_text(encoding="utf-8")
            except Exception:
                pass
        transcript_len = 0
        srt = d / "transcript.srt"
        if srt.exists():
            try:
                for line in srt.read_text(encoding="utf-8").split("\n"):
                    line = line.strip()
                    if not line or line.isdigit() or "-->" in line:
                        continue
                    transcript_len += len(line)
            except Exception:
                pass
        videos.append({
            "bvid": meta.get("bvid"),
            "title": meta.get("title", "?"),
            "author": meta.get("author", meta.get("uploader", "?")),
            "created_ts": meta.get("created", 0),
            "created_str": meta.get("created_str", "?"),
            "source": f"local:{meta.get('source', '?')}",
            "summary": summary,
            "transcript_len": transcript_len,
            "duration_str": "?",
            "url": f"https://www.bilibili.com/video/{meta.get('bvid')}",
        })
    return videos


# ─── 数据源: B 站 API ─────────────────────────────────────

def _get_cred():
    if not BILIBILI_API_OK:
        return None
    if not has_credential():
        return None
    cd = load_credential()
    return Credential(
        sessdata=cd.get("sessdata", ""),
        bili_jct=cd.get("bili_jct", ""),
        buvid3=cd.get("buvid3", ""),
        dedeuserid=cd.get("dedeuserid", ""),
    )


async def load_favorites(cred) -> List[Dict]:
    from bilibili_api.favorite_list import (
        get_video_favorite_list,
        get_video_favorite_list_content,
    )
    if not cred:
        return []
    cd = load_credential()
    uid = int(cd.get("dedeuserid", 0))
    if not uid:
        return []
    folders = await get_video_favorite_list(uid=uid, credential=cred)
    out = []
    for f in (folders.get("list") or []):
        fid = f.get("id")
        fname = f.get("title", "?")
        if not fid:
            continue
        page = 1
        while True:
            try:
                r = await get_video_favorite_list_content(
                    media_id=fid, page=page, credential=cred
                )
            except Exception as e:
                print(f"  ⚠️ fav {fname} page {page}: {e}", file=sys.stderr)
                break
            medias = r.get("medias") or []
            if not medias:
                break
            for m in medias:
                ts = m.get("fav_time", 0)
                out.append({
                    "bvid": m.get("bv_id") or m.get("bvid"),
                    "title": m.get("title", "?"),
                    "author": m.get("upper", {}).get("name", "?"),
                    "created_ts": ts,
                    "created_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?",
                    "source": f"favorites:{fname}",
                    "summary": "",
                    "transcript_len": 0,
                    "duration_str": f"{m.get('duration', 0)}s" if m.get("duration") else "?",
                    "url": f"https://www.bilibili.com/video/{m.get('bv_id') or m.get('bvid')}",
                })
            info = r.get("info", {})
            total = info.get("media_count", 0)
            if page * 20 >= total:
                break
            page += 1
    return out


async def load_watch_later(cred) -> List[Dict]:
    if not cred:
        return []
    import aiohttp
    cd = load_credential()
    sessdata = cd.get("sessdata", "")
    out = []
    page = 1
    while True:
        url = "https://api.bilibili.com/x/v2/history/toview"
        params = {"pn": page, "ps": 50}
        headers = {
            "User-Agent": "Mozilla/5.0 Chrome/120 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
            "Cookie": f"SESSDATA={sessdata}",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=15) as r:
                    data = await r.json(content_type=None)
        except Exception as e:
            print(f"  ⚠️ watch_later page {page}: {e}", file=sys.stderr)
            break
        if data.get("code") != 0:
            break
        items = (data.get("data") or {}).get("list") or []
        if not items:
            break
        for v in items:
            bvid = v.get("bvid")
            if not bvid:
                continue
            ts = v.get("add_at", 0)
            out.append({
                "bvid": bvid,
                "title": v.get("title", "?"),
                "author": v.get("owner", {}).get("name", "?"),
                "created_ts": ts,
                "created_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?",
                "source": "watch_later",
                "summary": "",
                "transcript_len": 0,
                "duration_str": f"{v.get('duration', 0)}s" if v.get("duration") else "?",
                "url": f"https://www.bilibili.com/video/{bvid}",
            })
        if len(items) < 50:
            break
        page += 1
    return out


async def load_followings(cred) -> List[Dict]:
    if not cred:
        return []
    from bilibili_api.user import get_user_videos
    import aiohttp
    cd = load_credential()
    uid = int(cd.get("dedeuserid", 0))
    if not uid:
        return []
    out = []
    pn = 1
    async with aiohttp.ClientSession() as session:
        headers = {
            "User-Agent": "Mozilla/5.0 Chrome/120 Safari/537.36",
            "Referer": "https://space.bilibili.com/",
            "Cookie": f"SESSDATA={cd.get('sessdata', '')}",
        }
        while True:
            url = "https://api.bilibili.com/x/relation/followings"
            params = {"vmid": uid, "pn": pn, "ps": 50, "order": "desc"}
            try:
                async with session.get(url, params=params, headers=headers, timeout=15) as r:
                    data = await r.json(content_type=None)
            except Exception as e:
                print(f"  ⚠️ followings page {pn}: {e}", file=sys.stderr)
                break
            if data.get("code") != 0:
                break
            items = (data.get("data") or {}).get("list") or []
            if not items:
                break
            for f in items:
                f_uid = f.get("mid")
                f_name = f.get("uname", "?")
                try:
                    r2 = await get_user_videos(uid=f_uid, credential=cred)
                except Exception:
                    continue
                vlist = (r2.get("list") or {}).get("vlist") or []
                for v in vlist[:5]:
                    ts = v.get("created", 0)
                    out.append({
                        "bvid": v.get("bvid"),
                        "title": v.get("title", "?"),
                        "author": f_name,
                        "created_ts": ts,
                        "created_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?",
                        "source": f"followings:{f_name}",
                        "summary": "",
                        "transcript_len": 0,
                        "duration_str": f"{v.get('length', '?')}",
                        "url": f"https://www.bilibili.com/video/{v.get('bvid')}",
                    })
            if len(items) < 50:
                break
            pn += 1
    return out


# ─── B 站 API: 写操作 ────────────────────────────────────

async def create_fav_folder(cred, name: str, intro: str = "") -> int:
    if not cred:
        raise RuntimeError("未登录")
    import aiohttp
    cd = load_credential()
    url = "https://api.bilibili.com/x/v3/fav/folder/add"
    headers = {
        "User-Agent": "Mozilla/5.0 Chrome/120 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Cookie": f"SESSDATA={cd.get('sessdata', '')}; bili_jct={cd.get('bili_jct', '')}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "title": name,
        "intro": intro,
        "privacy": 0,
        "cover": "",
        "csrf": cd.get("bili_jct", ""),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=data, headers=headers, timeout=15) as r:
            resp = await r.json(content_type=None)
    if resp.get("code") != 0:
        raise RuntimeError(f"创建收藏夹失败: {resp.get('message', resp)}")
    return int(resp["data"]["id"])


async def add_video_to_fav(cred, media_id: int, bvid: str) -> bool:
    if not cred:
        raise RuntimeError("未登录")
    import aiohttp
    from bilibili_api.video import Video
    cd = load_credential()
    url = "https://api.bilibili.com/x/v3/fav/resource/deal"
    headers = {
        "User-Agent": "Mozilla/5.0 Chrome/120 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Cookie": f"SESSDATA={cd.get('sessdata', '')}; bili_jct={cd.get('bili_jct', '')}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    v = Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    aid = info["aid"]
    data = {
        "rid": aid,
        "type": 2,
        "add_media_ids": str(media_id),
        "del_media_ids": "",
        "csrf": cd.get("bili_jct", ""),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=data, headers=headers, timeout=15) as r:
            resp = await r.json(content_type=None)
    if resp.get("code") != 0:
        raise RuntimeError(f"添加失败: {resp.get('message', resp)}")
    return True


# ─── LLM 评估 ───────────────────────────────────────────

def evaluate_video(client_cfg: Dict, standard: str, v: Dict) -> Dict:
    prompts = [
        ("full", lambda: build_prompt(standard, v)),
        ("simple", lambda: build_simple_prompt(standard, v)),
    ]
    for label, prompt_fn in prompts:
        for attempt in range(2):
            try:
                result = _llm_call(
                    client_cfg,
                    system="只输出 JSON, 无 markdown 包装。",
                    user=prompt_fn(),
                    max_tokens=300,
                )
                parsed = parse_llm_json(result)
                if parsed and parsed.get("score", 0) > 0:
                    parsed["_attempt"] = f"{label}#{attempt+1}"
                    return parsed
            except Exception:
                continue
    return {
        "score": 0,
        "tier": "skip",
        "reason": "LLM 返回空 (M2.7 偶发)",
        "topics": [],
        "_fallback": True,
    }


# ─── 报告 ───────────────────────────────────────────────

def format_entry(idx: int, result: Dict) -> List[str]:
    v = result["video"]
    e = result["eval"]
    lines = [
        f"### {idx}. `{v['bvid']}` - {v['title']}",
        f"- **UP主**: {v['author']}",
        f"- **来源**: `{v.get('source', '?')}`",
        f"- **日期**: {v.get('created_str', '?')}",
        f"- **评分**: **{e['score']}/12** ({e['tier']})",
        f"- **评估**: {e['reason']}",
    ]
    if e.get("topics"):
        topics = " ".join(f"`{t}`" for t in e["topics"])
        lines.append(f"- **主题**: {topics}")
    lines.append(f"- **链接**: {v.get('url', f'https://www.bilibili.com/video/{v[chr(34)+chr(98)+chr(118)+chr(105)+chr(100)+chr(34)]}')}")
    return lines


def generate_report(standard: str, results: List[Dict]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    high = [r for r in results if r["eval"]["tier"] == "high"]
    medium = [r for r in results if r["eval"]["tier"] == "medium"]
    skip = [r for r in results if r["eval"]["tier"] == "skip"]
    lines = [
        f"# AI 收藏夹建议 - {today}\n",
        "## 📊 评估概览\n",
        f"- **评估视频数**: {len(results)}",
        f"- **建议收藏**: {len(high) + len(medium)} (高={len(high)}, 中={len(medium)})",
        f"- **标准**: {standard}",
        f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
    ]
    if high:
        lines.append("## 🌟 高价值 (建议收藏)\n")
        for i, r in enumerate(sorted(high, key=lambda x: -x["eval"]["score"]), 1):
            lines.extend(format_entry(i, r))
        lines.append("")
    if medium:
        lines.append("## 💡 中价值 (可选)\n")
        for i, r in enumerate(sorted(medium, key=lambda x: -x["eval"]["score"]), 1):
            lines.extend(format_entry(i, r))
        lines.append("")
    if skip:
        lines.append(f"## ⚪ 跳过 ({len(skip)} 个, 仅显示前 10)\n")
        for r in sorted(skip, key=lambda x: -x["eval"]["score"])[:10]:
            v = r["video"]
            e = r["eval"]
            lines.append(f"- `{v['bvid']}` {v['title'][:50]}... (score={e['score']}, {e['reason']})")
        if len(skip) > 10:
            lines.append(f"- ... 及其他 {len(skip) - 10} 个\n")
        else:
            lines.append("")
    lines.extend([
        "## 🛠️ 如何执行\n",
        "本工具**默认 dry-run**, 不会自动调 B 站 API。\n",
        "### 方案 A: 手动收藏 (零风险)\n",
        f"1. 在 B 站网页端创建收藏夹 `AI精选-{today}`",
        "2. 点击下方链接逐个收藏\n",
        f"```\n{' '.join(r['video']['bvid'] for r in sorted(high, key=lambda x: -x['eval']['score']))}\n```\n",
        "### 方案 B: 调 B 站 API 批量 (限频 + 风险)\n",
        "```bash",
        f"python3 bilibili_curator.py --source all --standard \"{standard}\" --execute --folder-name \"AI精选-{today}\"",
        "```\n",
        "⚠️  **风险**: 创建收藏夹 + 移入视频是 B 站 ToS 灰色地带, 限频 2-3 秒/次。\n",
        "---\n",
        "*Generated by bilibili-curator v2.0*",
    ])
    return "\n".join(lines)


# ─── 主流程 ───────────────────────────────────────────────

async def main_async(args):
    downloads_dir = Path(args.downloads)
    source = args.source
    mode = args.mode
    standard = args.standard

    print(f"📂 数据源: {source}", file=sys.stderr)
    all_videos: List[Dict] = []
    cred = _get_cred() if source in ("favorites", "watch_later", "followings", "all") else None

    if source in ("local", "all"):
        local = load_local(downloads_dir)
        print(f"  local: {len(local)}", file=sys.stderr)
        all_videos.extend(local)
    if source in ("favorites", "all"):
        if not cred:
            print("  ❌ favorites: 未登录", file=sys.stderr)
        else:
            fav = await load_favorites(cred)
            print(f"  favorites: {len(fav)}", file=sys.stderr)
            all_videos.extend(fav)
    if source in ("watch_later", "all"):
        if not cred:
            print("  ❌ watch_later: 未登录", file=sys.stderr)
        else:
            wl = await load_watch_later(cred)
            print(f"  watch_later: {len(wl)}", file=sys.stderr)
            all_videos.extend(wl)
    if source in ("followings", "all"):
        if not cred:
            print("  ❌ followings: 未登录", file=sys.stderr)
        else:
            fl = await load_followings(cred)
            print(f"  followings: {len(fl)}", file=sys.stderr)
            all_videos.extend(fl)

    seen = set()
    deduped = []
    for v in all_videos:
        bvid = v.get("bvid")
        if not bvid or bvid in seen:
            continue
        seen.add(bvid)
        deduped.append(v)
    print(f"📊 去重后: {len(deduped)}", file=sys.stderr)

    if not deduped:
        print("⚠️  无视频", file=sys.stderr)
        return

    if args.dry_run:
        print(f"DRY-RUN: 会调 LLM {len(deduped)} 次", file=sys.stderr)
        return

    try:
        client_cfg = _get_llm_client()
        print(f"🤖 LLM: {client_cfg['model']}", file=sys.stderr)
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    results = []
    for i, v in enumerate(deduped, 1):
        if i % 10 == 0 or i == len(deduped):
            print(f"  评估 {i}/{len(deduped)}", file=sys.stderr)
        results.append({"video": v, "eval": evaluate_video(client_cfg, standard, v)})
    results.sort(key=lambda r: -r["eval"]["score"])

    report = generate_report(standard, results)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"📝 报告: {args.output}", file=sys.stderr)
    else:
        print(report)

    if mode == "execute":
        if not cred:
            print("❌ execute 模式需要登录", file=sys.stderr)
            sys.exit(1)
        if not args.folder_name:
            args.folder_name = f"AI精选-{today}"
        high = [r for r in results if r["eval"]["tier"] == "high"]
        if not high:
            print("⚠️ 无 high 价值视频, 不创建收藏夹", file=sys.stderr)
            return
        print(f"\n🚀 Execute: 创建 '{args.folder_name}', 移入 {len(high)} 个", file=sys.stderr)
        if not args.yes:
            resp = input("确认? (yes/no): ")
            if resp.strip().lower() != "yes":
                print("取消", file=sys.stderr)
                return
        try:
            media_id = await create_fav_folder(cred, args.folder_name, intro=f"AI 评估 {today}")
            print(f"✅ 创建: {args.folder_name} (id={media_id})", file=sys.stderr)
        except Exception as e:
            print(f"❌ 创建失败: {e}", file=sys.stderr)
            return
        success = 0
        for r in high:
            bvid = r["video"]["bvid"]
            try:
                await add_video_to_fav(cred, media_id, bvid)
                success += 1
                print(f"  ✅ {bvid} {r['video']['title'][:30]}", file=sys.stderr)
                time.sleep(2.5)
            except Exception as e:
                print(f"  ❌ {bvid}: {e}", file=sys.stderr)
        print(f"\n🎉 完成: {success}/{len(high)} 已加入 '{args.folder_name}'", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="B 站 AI 收藏夹助手 (Python 端)")
    parser.add_argument("--source", choices=["local", "favorites", "watch_later", "followings", "all"], default="local")
    parser.add_argument("--downloads", default=str(DEFAULT_DOWNLOADS))
    parser.add_argument("--standard", default="综合价值 (内容深度 + 时效性 + 实用性)")
    parser.add_argument("--mode", choices=["dry-run", "execute"], default="dry-run")
    parser.add_argument("--folder-name", default=None)
    parser.add_argument("--output", "-o", default=None)
    parser.add_argument("--dry-run", action="store_true", help="只统计, 不调 LLM")
    parser.add_argument("-y", "--yes", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
