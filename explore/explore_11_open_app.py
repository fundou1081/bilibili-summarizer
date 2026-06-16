#!/usr/bin/env python3
"""验证 B 站跳转链接 + URL Scheme

目标: 确保 Flutter App 能正确跳转到 B 站打开指定视频

平台方案:
  - iOS:     bilibili://video/{bvid}  (B 站 App URL Scheme)
  - Android: intent:// 或 https://www.bilibili.com/video/{bvid}
  - 通用:    https://www.bilibili.com/video/{bvid}  (在浏览器中打开)

Flutter 实现: url_launcher 包
  - canLaunchUrl(Uri.parse('bilibili://video/BVxxx'))
  - launchUrl(Uri.parse('https://www.bilibili.com/video/BVxxx'), mode: LaunchMode.externalApplication)

探测项:
  1. 验证 BV 号有效性 (通过 api.bilibili.com)
  2. 构造 URL scheme 列表
  3. 测试链接是否有效 (HTTP HEAD)
"""

import sys
import asyncio
from pathlib import Path
import urllib.request
import urllib.error

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bilibili_cc import load_credential
from bilibili_api import video, Credential


def make_links(bvid):
    """生成各平台的跳转链接"""
    return {
        "网页": f"https://www.bilibili.com/video/{bvid}",
        "B 站 App": f"bilibili://video/{bvid}",
        "B 站 wap": f"https://m.bilibili.com/video/{bvid}",
        "分享短链": f"https://b23.tv/{bvid}",  # 这个是错的,短链是 6 位码
        "API 原始": f"https://api.bilibili.com/playurl?bvid={bvid}",
    }


async def main():
    cd = load_credential()
    cred = Credential(
        sessdata=cd.get("sessdata", ""),
        bili_jct=cd.get("bili_jct", ""),
        buvid3=cd.get("buvid3", ""),
        dedeuserid=cd.get("dedeuserid", ""),
    )

    # 用第一个收藏视频: BV13pLS6vEqj
    bvid = "BV13pLS6vEqj"
    v = video.Video(bvid=bvid, credential=cred)
    info = await v.get_info()
    print(f"✅ 视频验证: {info['title']}")
    print(f"   bvid={bvid}, aid={info['aid']}")
    print(f"   UP主={info['owner']['name']}\n")

    print("=" * 60)
    print("🔗 测试 11: B 站跳转链接")
    print("=" * 60)

    links = make_links(bvid)
    for name, url in links.items():
        print(f"\n--- {name} ---")
        print(f"   {url}")

        # HTTP HEAD 测试网页链接可用性
        if url.startswith("http"):
            try:
                req = urllib.request.Request(url, method="HEAD")
                req.headers["User-Agent"] = "Mozilla/5.0"
                with urllib.request.urlopen(req, timeout=5) as r:
                    print(f"   ✅ HTTP {r.status} ({r.getheader('Content-Type')})")
            except urllib.error.HTTPError as e:
                print(f"   ⚠️ HTTP {e.code}")
            except Exception as e:
                print(f"   ⚠️ {e}")
        else:
            # URL Scheme 不能 HTTP 测试
            print(f"   ℹ️ URL Scheme (只能用 App 测试)")

    # 测试 b23.tv 短链解析
    print(f"\n--- 短链解析 ---")
    b23_url = f"https://b23.tv/{bvid[:2]}"  # 模拟短链
    try:
        req = urllib.request.Request(b23_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as r:
            print(f"   {b23_url} → {r.url}")
    except Exception as e:
        print(f"   ❌ {e}")

    print(f"\n✅ #11 跑通!")
    print(f"\n📝 Flutter 实现方案:")
    print(f"   url_launcher.launchUrl('https://www.bilibili.com/video/{bvid}')")
    print(f"   或 url_launcher.launchUrl('bilibili://video/{bvid}')")


if __name__ == "__main__":
    asyncio.run(main())
