#!/usr/bin/env python3
"""
B站 视频评论 下载 + 过滤 工具
用法:
  python3 bilibili_comments.py -d https://www.bilibili.com/video/BVxxxxxx              # 默认 100 条
  python3 bilibili_comments.py -d -n 500 -m random URL                                    # 随机 500 条
  python3 bilibili_comments.py -d -p 2 URL                                                 # 只拉 P2 评论
  python3 bilibili_comments.py -d --min-length 4 --filter-digits --filter-dup URL        # 过滤选项
  python3 bilibili_comments.py -d -o ./my_comments URL                                     # 指定输出目录

跟 bilibili_cc.py / Flutter 一致的过滤选项:
  --min-length N     过滤短内容 (默认 0 = 不过滤)
  --filter-digits    过滤纯数字/标点
  --filter-dup       过滤重复 (前 20 字去重)
"""

import sys
import os
import re
import json
import asyncio
import argparse
import random
from datetime import datetime
from xml.etree import ElementTree as ET

# 复用 bilibili_cc 的凭据管理
from bilibili_cc import load_credential, CREDENTIAL_FILE


def make_credential(cred_data: dict):
    """从 dict 创建 bilibili_api Credential"""
    from bilibili_api import Credential
    return Credential(
        sessdata=cred_data.get("sessdata", ""),
        bili_jct=cred_data.get("bili_jct", ""),
        dedeuserid=cred_data.get("dedeuserid", ""),
        ac_time_value=cred_data.get("ac_time_value", ""),
        buvid3=cred_data.get("buvid3", ""),
        buvid4=cred_data.get("buvid4", ""),
    )


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


# ─── 评论拉取 ───────────────────────────────────────────────────────

async def fetch_comments_async(aid: int, credential, max_count: int = 100,
                                mode: str = "first") -> list:
    """拉评论 (含子评论)
    
    Args:
        aid: 视频 aid
        credential: bilibili_api Credential
        max_count: 最大条数 (主评论)
        mode: 'first' = 按时间顺序, 'random' = 随机
    
    Returns:
        list of comment dicts
    """
    from bilibili_api import comment

    all_comments = []
    page = 1
    ps = 20  # 每页 20 条 (B站 API 上限)
    max_pages = (max_count + ps - 1) // ps

    print(f"   拉评论: aid={aid}, mode={mode}, max={max_count}")
    while len(all_comments) < max_count and page <= max_pages:
        try:
            result = await comment.get_comments(
                oid=aid,
                type_=comment.CommentResourceType.VIDEO,
                page_index=page,
                credential=credential,
            )
        except Exception as e:
            print(f"   第 {page} 页失败: {e}")
            break

        replies = result.get("replies", []) or []
        if not replies:
            break

        for r in replies:
            cmt = {
                "rpid": r.get("rpid"),
                "uname": r.get("member", {}).get("uname", "?"),
                "level": r.get("member", {}).get("level_info", {}).get("current_level", 0),
                "content": r.get("content", {}).get("message", ""),
                "like": r.get("like", 0),
                "ctime": r.get("ctime", 0),
                "replies": [],
            }
            # 子评论
            for sub in r.get("replies", []) or []:
                cmt["replies"].append({
                    "uname": sub.get("member", {}).get("uname", "?"),
                    "content": sub.get("content", {}).get("message", ""),
                    "like": sub.get("like", 0),
                })
            all_comments.append(cmt)

        print(f"   第 {page} 页: +{len(replies)} (累计 {len(all_comments)})")

        # 没更多了
        if len(replies) < ps:
            break
        page += 1
        # API 限速: 0.5s/页
        await asyncio.sleep(0.5)

    # 采样
    if mode == "random" and len(all_comments) > max_count:
        all_comments = random.sample(all_comments, max_count)
    elif len(all_comments) > max_count:
        all_comments = all_comments[:max_count]

    return all_comments


# ─── 过滤 ──────────────────────────────────────────────────────────

def filter_comments(comments: list, min_length: int = 0,
                     filter_digits: bool = False, filter_dup: bool = False) -> list:
    """过滤评论"""
    result = comments
    if min_length > 0:
        before = len(result)
        result = [c for c in result if len(c["content"]) >= min_length]
        print(f"   过滤短内容 (≥{min_length} 字): {before} → {len(result)}")

    if filter_digits:
        before = len(result)
        pat = re.compile(r'^[\d\?\!\.\s]+$')
        result = [c for c in result if not pat.match(c["content"].strip())]
        print(f"   过滤纯数字/标点: {before} → {len(result)}")

    if filter_dup:
        before = len(result)
        seen = set()
        unique = []
        for c in result:
            key = c["content"][:20]  # 前 20 字去重
            if key in seen:
                continue
            seen.add(key)
            unique.append(c)
        result = unique
        print(f"   过滤重复 (前 20 字): {before} → {len(result)}")

    return result


# ─── 主流程 ────────────────────────────────────────────────────────

async def download_comments_async(url: str, output_dir: str = "",
                                  max_count: int = 100, mode: str = "first",
                                  min_length: int = 0, filter_digits: bool = False,
                                  filter_dup: bool = False, page: int = 0) -> int:
    """主流程"""
    from bilibili_api import video

    # 凭据
    cred_data = load_credential()
    if not cred_data.get("sessdata"):
        print("[ERROR] 请先运行: python3 bilibili_cc.py --login")
        return 1
    credential = make_credential(cred_data)

    # BVID
    bvid = extract_bvid(url)
    if page == 0:
        page = extract_pid(url)

    print(f"获取视频信息: {bvid} (P{page}) ...")
    try:
        v = video.Video(bvid=bvid, credential=credential)
        info = await v.get_info()
    except Exception as e:
        print(f"[ERROR] 获取视频信息失败: {e}")
        return 1

    title = info.get("title", "unknown")
    pages = info.get("pages", [])

    # 取指定分P 的 aid
    if page <= 1 or page > len(pages):
        aid = info.get("aid")
        cid = info.get("cid")
    else:
        page_data = pages[page - 1]
        aid = info.get("aid")
        cid = page_data.get("cid")

    print(f"标题: {title}")
    print(f"分P: P{page}/{len(pages)} | aid={aid} | cid={cid}\n")

    # 拉评论
    comments = await fetch_comments_async(aid, credential, max_count, mode)
    print(f"\n📊 拉取完成: {len(comments)} 条")

    # 过滤
    if min_length > 0 or filter_digits or filter_dup:
        print("\n🔧 过滤:")
        comments = filter_comments(comments, min_length, filter_digits, filter_dup)
        print(f"   过滤后: {len(comments)} 条\n")

    # 统计
    if comments:
        total_chars = sum(len(c["content"]) for c in comments)
        total_replies = sum(len(c["replies"]) for c in comments)
        print(f"📈 统计:")
        print(f"   总字符: {total_chars}")
        print(f"   平均: {total_chars // len(comments)} 字/评论")
        print(f"   子评论: {total_replies} 条")

        # Top 3
        top = sorted(comments, key=lambda c: c["like"], reverse=True)[:3]
        print(f"\n🔥 Top 3 高赞评论:")
        for c in top:
            content = c["content"][:120].replace("\n", " ")
            print(f"   👍 {c['like']} · {c['uname']}: {content}")

    # 保存
    if not output_dir:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads", bvid)
    os.makedirs(output_dir, exist_ok=True)

    # 文件名: P{page}-comments-{mode}.json
    suffix = f"comments-{mode}"
    if min_length > 0:
        suffix += f"-min{min_length}"
    if filter_digits:
        suffix += "-nodigit"
    if filter_dup:
        suffix += "-nodup"
    output_file = os.path.join(output_dir, f"P{page}-{suffix}.json")

    save_data = {
        "bvid": bvid,
        "aid": aid,
        "cid": cid,
        "page": page,
        "title": title,
        "mode": mode,
        "filters": {
            "min_length": min_length,
            "filter_digits": filter_digits,
            "filter_dup": filter_dup,
        },
        "fetched_at": datetime.now().isoformat(),
        "count": len(comments),
        "comments": comments,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    print(f"\n💾 已保存: {output_file}")
    print(f"   {len(comments)} 条评论, {os.path.getsize(output_file) // 1024} KB")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="B站 视频评论 下载 + 过滤 工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s -d https://www.bilibili.com/video/BV1xx411c7mD                # 默认 100 条 (按时间)
  %(prog)s -d -n 500 -m random URL                                       # 随机 500 条
  %(prog)s -d -p 2 URL                                                   # 只拉 P2 评论
  %(prog)s -d --min-length 4 --filter-digits --filter-dup URL            # 过滤: ≥4字 + 纯数字 + 重复
  %(prog)s -d -o ./my_comments URL                                       # 指定输出目录
        """,
    )
    parser.add_argument("-d", action="store_true", help="下载评论")
    parser.add_argument("-n", type=int, default=100, help="最大条数 (默认 100)")
    parser.add_argument("-m", "--mode", choices=["first", "random"], default="first",
                        help="采样方式 (默认 first=按时间, random=随机)")
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

    code = asyncio.run(download_comments_async(
        url=args.input,
        output_dir=args.output_dir,
        max_count=args.n,
        mode=args.mode,
        min_length=args.min_length,
        filter_digits=args.filter_digits,
        filter_dup=args.filter_dup,
        page=args.page,
    ))
    return code


if __name__ == "__main__":
    sys.exit(main())