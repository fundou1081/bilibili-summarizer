#!/usr/bin/env python3
"""
B站 视频弹幕 下载 + 过滤 工具
用法:
  python3 bilibili_danmaku.py -d https://www.bilibili.com/video/BVxxxxxx              # 默认 200 条
  python3 bilibili_danmaku.py -d -n 1000 URL                                          # 1000 条
  python3 bilibili_danmaku.py -d -p 2 URL                                             # 只拉 P2 弹幕
  python3 bilibili_danmaku.py -d --min-length 4 --filter-digits --filter-dup URL      # 过滤选项

跟 Flutter app 一致的过滤选项:
  --min-length N     过滤短内容 (默认 0 = 不过滤)
  --filter-digits    过滤纯数字/标点
  --filter-dup       过滤重复 (前 20 字去重)

弹幕源: https://comment.bilibili.com/{cid}.xml (XML API, 无需 protobuf)
"""

import sys
import os
import re
import json
import asyncio
import argparse
from datetime import datetime
from urllib.parse import urlparse

# 复用 bilibili_cc 的凭据管理
from bilibili_cc import load_credential

try:
    import aiohttp
except ImportError:
    print("[ERROR] 缺少 aiohttp, 请运行: pip install aiohttp")
    sys.exit(1)


def extract_bvid(url: str) -> str:
    """从 URL 中提取 BVID"""
    m = re.search(r'BV[A-Za-z0-9]+', url)
    if not m:
        raise ValueError(f"无法从 URL 提取 BVID: {url}")
    return m.group(0)


def extract_pid(url: str) -> int:
    """从 URL 中提取分P (默认 1)"""
    m = re.search(r'[?&]p=(\d+)', url)
    return int(m.group(1)) if m else 1


# ─── 视频信息 ──────────────────────────────────────────────────────

async def get_video_info_async(bvid: str, page: int = 1) -> dict:
    """获取视频信息 (不需要登录, 用 web 接口)
    
    Returns: dict with keys: aid, cid, title, pages
    """
    import aiohttp

    url = "https://api.bilibili.com/x/web-interface/view"
    params = {"bvid": bvid}

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com/",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise Exception(f"B站 API HTTP {resp.status}")
            data = await resp.json()
            if data.get("code") != 0:
                raise Exception(f"B站 API 错误 {data.get('code')}: {data.get('message')}")
            info = data["data"]

    # 取指定分P 的 cid
    pages = info.get("pages", [])
    if page <= 1 or page > len(pages):
        cid = info.get("cid")
    else:
        cid = pages[page - 1].get("cid")

    return {
        "aid": info.get("aid"),
        "cid": cid,
        "title": info.get("title", "unknown"),
        "pages": pages,
    }


# ─── 弹幕 XML 解析 ────────────────────────────────────────────────

def parse_danmaku_xml(xml_text: str) -> list:
    """解析弹幕 XML → list of dicts
    
    格式: <d p="time,type,size,color,unix_time,pool,sender_hash,id">内容</d>
    """
    danmaku = []
    # 匹配 <d p="...">...</d>
    pattern = re.compile(r'<d\s+p="([^"]+)"[^>]*>([^<]*)</d>')
    for m in pattern.finditer(xml_text):
        attrs = m.group(1)
        content = m.group(2)
        parts = attrs.split(",")
        if len(parts) < 8:
            continue
        try:
            time_sec = float(parts[0])  # 秒 (带小数)
            send_time = int(parts[4])   # unix sec
            danmaku_id = int(parts[7])
            color = int(parts[3])
            progress_ms = int(time_sec * 1000)  # 秒 → 毫秒

            danmaku.append({
                "id": danmaku_id,
                "progress": progress_ms,
                "time": send_time,
                "content": content.strip(),
                "color": color,
            })
        except (ValueError, IndexError):
            continue

    # 按时间排序
    danmaku.sort(key=lambda d: d["progress"])
    return danmaku


async def fetch_danmaku_async(cid: int) -> list:
    """从 B站 XML API 拉弹幕
    
    API: https://comment.bilibili.com/{cid}.xml
    """
    url = f"https://comment.bilibili.com/{cid}.xml"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com/",
    }

    print(f"   拉弹幕: cid={cid}")
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status}")
            xml_text = await resp.text(encoding="utf-8")

    if not xml_text:
        raise Exception("空响应")

    danmaku = parse_danmaku_xml(xml_text)
    return danmaku


# ─── 过滤 ──────────────────────────────────────────────────────────

def filter_danmaku(danmaku: list, min_length: int = 0,
                    filter_digits: bool = False, filter_dup: bool = False) -> list:
    """过滤弹幕"""
    result = danmaku
    if min_length > 0:
        before = len(result)
        result = [d for d in result if len(d["content"]) >= min_length]
        print(f"   过滤短内容 (≥{min_length} 字): {before} → {len(result)}")

    if filter_digits:
        before = len(result)
        pat = re.compile(r'^[\d\?\!\.\s]+$')
        result = [d for d in result if not pat.match(d["content"].strip())]
        print(f"   过滤纯数字/标点: {before} → {len(result)}")

    if filter_dup:
        before = len(result)
        seen = set()
        unique = []
        for d in result:
            key = d["content"][:20]
            if key in seen:
                continue
            seen.add(key)
            unique.append(d)
        result = unique
        print(f"   过滤重复 (前 20 字): {before} → {len(result)}")

    return result


# ─── 主流程 ────────────────────────────────────────────────────────

async def download_danmaku_async(url: str, output_dir: str = "",
                                  max_count: int = 200,
                                  min_length: int = 0, filter_digits: bool = False,
                                  filter_dup: bool = False, page: int = 0) -> int:
    """主流程"""
    bvid = extract_bvid(url)
    if page == 0:
        page = extract_pid(url)

    # 拿视频信息 (拿 cid)
    print(f"获取视频信息: {bvid} (P{page}) ...")
    try:
        info = await get_video_info_async(bvid, page)
    except Exception as e:
        print(f"[ERROR] 获取视频信息失败: {e}")
        return 1

    cid = info.get("cid", 0)
    if cid == 0:
        print("[ERROR] cid 为 0, 请检查视频是否存在")
        return 1

    print(f"标题: {info['title']}")
    print(f"分P: P{page}/{len(info['pages'])} | aid={info['aid']} | cid={cid}\n")

    # 拉弹幕
    try:
        danmaku = await fetch_danmaku_async(cid)
    except Exception as e:
        print(f"[ERROR] 拉弹幕失败: {e}")
        return 1

    print(f"\n📊 拉取完成: {len(danmaku)} 条")

    # 过滤
    if min_length > 0 or filter_digits or filter_dup:
        print("\n🔧 过滤:")
        danmaku = filter_danmaku(danmaku, min_length, filter_digits, filter_dup)
        print(f"   过滤后: {len(danmaku)} 条\n")

    # 采样
    if len(danmaku) > max_count:
        before = len(danmaku)
        danmaku = danmaku[:max_count]
        print(f"   截断到前 {max_count} 条: {before} → {len(danmaku)}")

    # 统计
    if danmaku:
        total_chars = sum(len(d["content"]) for d in danmaku)
        print(f"\n📈 统计:")
        print(f"   总字符: {total_chars}")
        print(f"   平均: {total_chars // len(danmaku)} 字/弹幕")
        print(f"   时间跨度: {danmaku[0]['progress']/1000:.0f}s ~ {danmaku[-1]['progress']/1000:.0f}s")

        # 时间分布
        total_ms = danmaku[-1]["progress"] - danmaku[0]["progress"] if len(danmaku) > 1 else 0
        if total_ms > 0:
            # 取 5 个时间段的密度
            seg = total_ms // 5
            segments = [0] * 5
            for d in danmaku:
                idx = min(4, (d["progress"] - danmaku[0]["progress"]) // seg) if seg > 0 else 0
                segments[idx] += 1
            print(f"\n   时间分布 (5 段):")
            for i, cnt in enumerate(segments):
                bar = "█" * (cnt * 30 // max(segments))
                print(f"     {bar} {cnt}")

    # 保存
    if not output_dir:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads", bvid)
    os.makedirs(output_dir, exist_ok=True)

    suffix = "danmaku"
    if min_length > 0:
        suffix += f"-min{min_length}"
    if filter_digits:
        suffix += "-nodigit"
    if filter_dup:
        suffix += "-nodup"
    output_file = os.path.join(output_dir, f"P{page}-{suffix}.json")

    save_data = {
        "bvid": bvid,
        "aid": info["aid"],
        "cid": cid,
        "page": page,
        "title": info["title"],
        "filters": {
            "min_length": min_length,
            "filter_digits": filter_digits,
            "filter_dup": filter_dup,
        },
        "fetched_at": datetime.now().isoformat(),
        "count": len(danmaku),
        "danmaku": danmaku,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    print(f"\n💾 已保存: {output_file}")
    print(f"   {len(danmaku)} 条弹幕, {os.path.getsize(output_file) // 1024} KB")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="B站 视频弹幕 下载 + 过滤 工具 (XML API, 无需 protobuf)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s -d https://www.bilibili.com/video/BV1xx411c7mD                # 默认 200 条
  %(prog)s -d -n 1000 URL                                                # 1000 条
  %(prog)s -d -p 2 URL                                                   # 只拉 P2 弹幕
  %(prog)s -d --min-length 4 --filter-digits --filter-dup URL            # 过滤: ≥4字 + 纯数字 + 重复
  %(prog)s -d -o ./my_danmaku URL                                        # 指定输出目录
        """,
    )
    parser.add_argument("-d", action="store_true", help="下载弹幕")
    parser.add_argument("-n", type=int, default=200, help="最大条数 (默认 200)")
    parser.add_argument("-p", "--page", type=int, default=0,
                        help="分P (0=从 URL 提取, 1+=指定分P)")
    parser.add_argument("--min-length", type=int, default=0,
                        help="过滤短内容最小字数 (默认 0=不过滤)")
    parser.add_argument("--filter-digits", action="store_true",
                        help="过滤纯数字/标点 (1234, ???)")
    parser.add_argument("--filter-dup", action="store_true",
                        help="过滤重复 (前 20 字去重)")
    parser.add_argument("-o", "--output-dir", default="",
                        help="输出目录 (默认 downloads/{bvid}/)")
    parser.add_argument("input", nargs="?", default="", help="B站视频 URL")

    args = parser.parse_args()

    if not args.d or not args.input:
        parser.print_help()
        return 0

    code = asyncio.run(download_danmaku_async(
        url=args.input,
        output_dir=args.output_dir,
        max_count=args.n,
        min_length=args.min_length,
        filter_digits=args.filter_digits,
        filter_dup=args.filter_dup,
        page=args.page,
    ))
    return code


if __name__ == "__main__":
    sys.exit(main())