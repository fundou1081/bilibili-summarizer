#!/usr/bin/env python3
"""探索 B 站关注列表

API: https://api.bilibili.com/x/relation/followings
参数: vmid, pn, ps (max 50)
"""

import sys
import asyncio
import time
import json
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bilibili_cc import load_credential

import aiohttp


def get_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Referer": "https://space.bilibili.com/",
        "Origin": "https://space.bilibili.com",
        "Accept": "application/json, text/plain, */*",
    }


def get_cookies():
    cd = load_credential()
    return {
        "SESSDATA": cd.get("sessdata", ""),
        "buvid3": cd.get("buvid3", ""),
    }


async def fetch_followings(uid, pn=1, ps=50):
    url = "https://api.bilibili.com/x/relation/followings"
    params = {
        "vmid": uid,
        "pn": pn,
        "ps": ps,
        "order": "desc",  # 按关注时间倒序
        "jsonp": "jsonp",
    }
    async with aiohttp.ClientSession(cookies=get_cookies(), headers=get_headers()) as session:
        async with session.get(url, params=params) as resp:
            # 用 content_type=None 避免 aiohttp 严格解析
            return await resp.json(content_type=None)


async def fetch_all_followings(uid, on_page=None):
    """分页拉完所有关注"""
    all_followings = []
    pn = 1
    while True:
        data = await fetch_followings(uid, pn=pn, ps=50)
        if data.get("code") != 0:
            print(f"   第 {pn} 页 code={data.get('code')}, msg={data.get('message')}")
            break
        lst = data["data"].get("list", [])
        total = data["data"].get("total", 0)
        if not lst:
            break
        all_followings.extend(lst)
        if on_page:
            on_page(pn, len(lst), total)
        if len(lst) < 50:
            break
        pn += 1
        if pn > 100:  # 安全阀 5000 个关注
            break
    return all_followings


async def main():
    cd = load_credential()
    if not cd.get("sessdata"):
        print("❌ 未登录")
        return

    uid = int(cd["dedeuserid"])
    print(f"✅ uid={uid}\n")

    print("=" * 60)
    print("👥 测试 3: 拉关注列表")
    print("=" * 60)

    # 先看第一页
    first = await fetch_followings(uid, pn=1, ps=10)
    if first.get("code") != 0:
        print(f"❌ {first.get('message')}")
        return

    total = first["data"]["total"]
    print(f"关注总数: {total}")
    print(f"前 10 个:")
    for f in first["data"]["list"][:10]:
        print(f"  👤 {f.get('uname')} (mid={f['mid']})")
        sign = f.get("sign", "")
        if sign:
            print(f"     签名: {sign[:60]}")
    print()

    # 性能测试 - 拉前 100 个
    print("--- 性能: 拉前 100 个 ---")
    start = time.time()
    sample = await fetch_all_followings(uid, on_page=lambda p, n, t: print(f"   第 {p} 页: +{n} (累计 {sum(x for x in [])})"))
    # 上面的 on_page 简单点
    elapsed = time.time() - start
    print(f"   拉了 {len(sample)} 个, 耗时 {elapsed:.1f}s")
    if sample:
        print(f"   速率: {len(sample)/max(0.1, elapsed):.0f} 个/秒")

    # 全部拉完
    if total > 100:
        print(f"\n--- 拉完全部 {total} 个关注 ---")
        start = time.time()
        # 实际拿全部
        all_data = await fetch_all_followings(uid, on_page=lambda p, n, t: None)
        elapsed = time.time() - start
        print(f"   总耗时: {elapsed:.1f}s")
        print(f"   速率: {len(all_data)/max(0.1, elapsed):.0f} 个/秒")

    print(f"\n✅ #3 跑通!")


if __name__ == "__main__":
    asyncio.run(main())
