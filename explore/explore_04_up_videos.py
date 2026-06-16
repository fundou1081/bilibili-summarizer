#!/usr/bin/env python3
"""探索 UP 主投稿列表

API: https://api.bilibili.com/x/space/wbi/arc/search
参数: mid, pn, ps (max 30), order, keyword
需要 WBI 签名
"""

import sys
import asyncio
import time
import json
import hashlib
import urllib.parse
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bilibili_cc import load_credential

import aiohttp


# WBI 签名实现 (跟 Flutter 端一致)
WBI_KEY_ORDER = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
]


def get_mixin_key(img_key, sub_key):
    s = img_key + sub_key
    return ''.join(s[i] for i in WBI_KEY_ORDER)


def wbi_sign(params, img_key, sub_key):
    sorted_params = sorted(params.items())
    encoded = []
    for k, v in sorted_params:
        encoded.append(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}")
    query = "&".join(encoded)
    mixin_key = get_mixin_key(img_key, sub_key)
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    params["w_rid"] = w_rid
    return params


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


async def get_wbi_keys(session):
    """从 nav 接口拿 img_key + sub_key"""
    url = "https://api.bilibili.com/x/web-interface/nav"
    async with session.get(url) as resp:
        data = await resp.json(content_type=None)
    if data.get("code") != 0:
        return None, None
    wbi_img = data["data"]["wbi_img"]
    img_url = wbi_img["img_url"]
    sub_url = wbi_img["sub_url"]
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    return img_key, sub_key


async def fetch_up_arc(mid, pn=1, ps=30, order="pubdate", keyword=""):
    """拿 UP 主投稿视频"""
    url = "https://api.bilibili.com/x/space/wbi/arc/search"
    async with aiohttp.ClientSession(cookies=get_cookies(), headers=get_headers()) as session:
        img_key, sub_key = await get_wbi_keys(session)
        if not img_key:
            return {"code": -1, "message": "wbi keys failed"}

        params = {
            "mid": mid,
            "pn": pn,
            "ps": ps,
            "order": order,
            "tid": 0,
            "keyword": keyword,
            "jsonp": "jsonp",
        }
        signed = wbi_sign(params, img_key, sub_key)
        async with session.get(url, params=signed) as resp:
            return await resp.json(content_type=None)


async def fetch_all_up_videos(mid, on_page=None):
    """分页拉完 UP 主所有视频"""
    all_videos = []
    pn = 1
    while True:
        data = await fetch_up_arc(mid, pn=pn, ps=30)
        if data.get("code") != 0:
            print(f"   第 {pn} 页 code={data.get('code')}, msg={data.get('message')}")
            break
        vlist = data.get("data", {}).get("list", {}).get("vlist", [])
        total = data.get("data", {}).get("page", {}).get("count", 0)
        if not vlist:
            break
        all_videos.extend(vlist)
        if on_page:
            on_page(pn, len(vlist), total)
        if len(vlist) < 30:
            break
        pn += 1
        if pn > 200:
            break
    return all_videos


async def main():
    cd = load_credential()
    if not cd.get("sessdata"):
        print("❌ 未登录")
        return

    # 测自己 (从 cookie 拿 uid)
    mid = cd.get("dedeuserid")
    print(f"✅ uid={mid} (用自己作为测试)\n")

    print("=" * 60)
    print(f"📺 测试 4: 拿 UP 主 (mid={mid}) 投稿列表")
    print("=" * 60)

    # 测试 3 种排序
    for order in ["pubdate", "click", "stow"]:
        print(f"\n--- 排序: {order} ---")
        result = await fetch_up_arc(mid, pn=1, ps=10, order=order)
        if result.get("code") != 0:
            print(f"❌ {result.get('message')}")
            continue
        data = result["data"]
        vlist = data.get("list", {}).get("vlist", [])
        total = data.get("page", {}).get("count", 0)
        print(f"  总数: {total}, 拿到: {len(vlist)}")
        for v in vlist[:3]:
            print(f"  📹 {v.get('title', '')[:40]}")
            print(f"     bvid={v.get('bvid')} | play={v.get('play')} | created={v.get('created')}")

    # 性能: 拉前 30 个
    print(f"\n--- 性能: 拉前 30 个 ---")
    start = time.time()
    vs = await fetch_all_up_videos(mid, on_page=lambda p, n, t: print(f"   第 {p} 页: +{n} (累计 /{t})"))
    elapsed = time.time() - start
    print(f"   耗时: {elapsed:.1f}s, 拿到 {len(vs)} 个")

    # 字段检查
    if vs:
        print(f"\n🔍 字段检查 (前 10 个):")
        for k in ['bvid', 'title', 'mid', 'author', 'play', 'created', 'length', 'pic']:
            cnt = sum(1 for v in vs if v.get(k))
            print(f"   {k}: {cnt}/{len(vs)}")

    print(f"\n✅ #4 跑通!")


if __name__ == "__main__":
    asyncio.run(main())
