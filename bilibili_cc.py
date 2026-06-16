#!/usr/bin/env python3
"""
B站 CC字幕下载 + 转换工具
用法:
  python3 bilibili_cc.py --login                             # 二维码登录 (首次使用)
  python3 bilibili_cc.py -d <URL>                            # 下载字幕 JSON
  python3 bilibili_cc.py -c <json_file>                      # 转换 JSON → SRT
  python3 bilibili_cc.py -c -d <URL>                         # 下载并转换 SRT
  python3 bilibili_cc.py -d -s 2 -e 5 <URL>                  # 下载 P2~P5
  python3 bilibili_cc.py -d -D ./subs <URL>                  # 指定输出目录
"""

import sys
import os
import re
import json
import asyncio
import argparse

CREDENTIAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".credential.json")


# ─── Credential 管理 ───────────────────────────────────────────────

def save_credential(cred: dict) -> None:
    with open(CREDENTIAL_FILE, "w") as f:
        json.dump(cred, f)
    os.chmod(CREDENTIAL_FILE, 0o600)
    print(f"✓ 凭据已保存到 {CREDENTIAL_FILE}")


def load_credential() -> dict:
    if os.path.exists(CREDENTIAL_FILE):
        with open(CREDENTIAL_FILE) as f:
            return json.load(f)
    return {}


def has_credential() -> bool:
    cred = load_credential()
    return bool(cred.get("sessdata"))


async def do_qrcode_login():
    """二维码登录 B站"""
    from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents
    from bilibili_api import Credential

    qr = QrCodeLogin()
    await qr.generate_qrcode()

    # 在终端打印二维码
    print("\n" + "=" * 50)
    print("请用 B站 App 扫描以下二维码登录:")
    print("=" * 50 + "\n")
    qr_url = qr._QrCodeLogin__qr_link
    print(f"二维码链接: {qr_url}\n")
    print(qr.get_qrcode_terminal())
    print("\n等待扫码...")

    while True:
        event = await qr.check_state()
        if event == QrCodeLoginEvents.SCAN:
            print("已扫描，请在 App 上确认登录...")
        elif event == QrCodeLoginEvents.CONF:
            # Web 登录区分扫描和确认，TV 不区分
            pass
        elif event == QrCodeLoginEvents.TIMEOUT:
            print("✗ 二维码已过期，请重新运行 --login")
            return None
        elif event == QrCodeLoginEvents.DONE:
            cred_obj: Credential = qr.get_credential()
            cred = {
                "sessdata": cred_obj.sessdata,
                "bili_jct": cred_obj.bili_jct,
                "dedeuserid": cred_obj.dedeuserid,
                "ac_time_value": cred_obj.ac_time_value,
                "buvid3": cred_obj.buvid3,
                "buvid4": cred_obj.buvid4,
            }
            save_credential(cred)
            print("✓ 登录成功!")
            return cred
        await asyncio.sleep(1)


# ─── 字幕下载 ─────────────────────────────────────────────────────

def time_convert(raw: str) -> str:
    try:
        total_sec = float(raw)
    except ValueError:
        total_sec = 0.0
    h = int(total_sec // 3600)
    m = int((total_sec % 3600) // 60)
    s = int(total_sec % 60)
    ms = int((total_sec - int(total_sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def convert_json_to_srt(json_path: str):
    output_path = re.sub(r'\.json$', '.srt', json_path)
    print(f"  转换: {os.path.basename(json_path)}  →  {os.path.basename(output_path)}")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    body = data.get("body", [])
    if not body:
        print(f"  [WARN] 空的字幕内容")
        return

    with open(output_path, "w", encoding="utf-8") as out:
        for i, item in enumerate(body, 1):
            from_time = time_convert(str(item["from"]))
            to_time = time_convert(str(item["to"]))
            content = item["content"].replace("\r\n", "\n").replace("\n", "\\N")
            out.write(f"{i}\n")
            out.write(f"{from_time} --> {to_time}\n")
            out.write(f"{content}\n\n")

    print(f"  ✓ 已生成: {output_path}")


async def download_subtitles_async(url: str, output_dir: str = "", p_start: int = 0,
                                    p_end: int = 0, auto_convert: bool = False):
    """使用 bilibili-api 下载字幕"""
    from bilibili_api import video, Credential
    import aiohttp

    cred_data = load_credential()
    if not cred_data.get("sessdata"):
        print("[ERROR] 请先运行 --login 登录 B站")
        return 1

    credential = Credential(
        sessdata=cred_data.get("sessdata"),
        bili_jct=cred_data.get("bili_jct"),
        dedeuserid=cred_data.get("dedeuserid"),
        ac_time_value=cred_data.get("ac_time_value"),
        buvid3=cred_data.get("buvid3"),
        buvid4=cred_data.get("buvid4"),
    )

    # 提取 bvid
    bvid_match = re.search(r'BV[A-Za-z0-9]+', url)
    if not bvid_match:
        print("[ERROR] 无法从 URL 提取 BVID")
        return 1
    bvid = bvid_match.group(0)

    print(f"获取视频信息: {bvid} ...")
    try:
        v = video.Video(bvid=bvid, credential=credential)
        info = await v.get_info()
    except Exception as e:
        print(f"[ERROR] 获取视频信息失败: {e}")
        return 1

    title = info.get("title", "unknown")
    pages = info.get("pages", [])
    total_pages = len(pages)
    print(f"标题: {title}")
    print(f"分P数: {total_pages}")

    # 确定下载范围
    pid_from_url = 1
    pid_match = re.search(r'[?&]p=(\d+)', url)
    if pid_match:
        pid_from_url = int(pid_match.group(1))

    if p_start == 0 and p_end == 0:
        p_start = p_end = pid_from_url
    else:
        if p_start == 0:
            p_start = 1
        if p_end == 0:
            p_end = total_pages
        p_start = max(1, p_start)
        p_end = min(p_end, total_pages)

    print(f"下载范围: P{p_start} ~ P{p_end}")

    if not output_dir:
        output_dir = f"downloads/{bvid}"
    os.makedirs(output_dir, exist_ok=True)

    total_files = 0
    async with aiohttp.ClientSession() as session:
        for pid in range(p_start, p_end + 1):
            if pid > len(pages):
                break

            page = pages[pid - 1]
            cid = page["cid"]
            page_title = page.get("part", f"P{pid}")

            # 获取字幕列表
            try:
                subtitle_info = await v.get_subtitle(cid)
            except Exception as e:
                print(f"  [WARN] P{pid} ({page_title}) 获取字幕失败: {e}")
                continue

            subtitles = subtitle_info.get("subtitles", [])
            if not subtitles:
                print(f"  [INFO] P{pid} ({page_title}) 无字幕")
                continue

            for sub in subtitles:
                lan = sub.get("lan_doc", sub.get("lan", "unknown"))
                subtitle_url = sub.get("subtitle_url", "")
                if not subtitle_url:
                    continue
                if subtitle_url.startswith("//"):
                    subtitle_url = "https:" + subtitle_url

                filename = f"P{pid}-{lan}.json"
                filepath = os.path.join(output_dir, filename)

                print(f"  ↓ 下载: {lan} → {filepath}")
                try:
                    async with session.get(subtitle_url) as resp:
                        resp.raise_for_status()
                        subtitle_content = await resp.json()

                    with open(filepath, "w", encoding="utf-8") as f:
                        json.dump(subtitle_content, f, ensure_ascii=False, indent=2)
                    total_files += 1

                    if auto_convert:
                        convert_json_to_srt(filepath)

                except Exception as e:
                    print(f"    [ERROR] 下载失败: {e}")

    if total_files == 0:
        print("\n未下载到任何字幕。")
        print("可能原因: 该视频没有 CC/AI 字幕，或需要刷新登录 (--login)")
    else:
        print(f"\n✓ 完成! 共 {total_files} 个字幕文件 → {output_dir}/")
    return 0


# ─── 命令行入口 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="B站 CC字幕下载 + SRT 转换工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --login                            # 首次使用: 二维码扫码登录
  %(prog)s -d https://www.bilibili.com/video/BV1xx411c7mD
  %(prog)s -d -s 1 -e 3 https://www.bilibili.com/video/BV1xx411c7mD
  %(prog)s -c downloads/xxx/P1-zh-CN.json     # JSON → SRT
  %(prog)s -c -d https://www.bilibili.com/video/BV1xx411c7mD  # 下载并转 SRT
        """
    )
    parser.add_argument("--login", action="store_true", help="二维码登录 B站 (保存凭据)")
    parser.add_argument("-d", action="store_true", help="下载字幕")
    parser.add_argument("-c", action="store_true", help="转换 JSON → SRT")
    parser.add_argument("-s", type=int, default=0, help="起始分P")
    parser.add_argument("-e", type=int, default=0, help="结束分P")
    parser.add_argument("-D", dest="output_dir", default="", help="输出目录")
    parser.add_argument("input", nargs="?", default="", help="B站视频URL 或 JSON文件")

    args = parser.parse_args()

    # --login 模式
    if args.login:
        asyncio.run(do_qrcode_login())
        return 0

    # 纯转换: -c <json_file>
    if args.c and not args.d and args.input:
        if args.input.endswith(".json"):
            convert_json_to_srt(args.input)
            return 0
        print("[ERROR] 转换模式需要 .json 文件")
        return 1

    # 下载模式: -d <URL>
    if args.d and args.input:
        code = asyncio.run(download_subtitles_async(
            url=args.input,
            output_dir=args.output_dir,
            p_start=args.s,
            p_end=args.e,
            auto_convert=args.c,
        ))
        return code

    # 无参数: 显示帮助 + 登录状态
    if has_credential():
        print("✓ 已登录 (凭据已保存)\n")
    else:
        print("⚠ 未登录，请先运行: python3 bilibili_cc.py --login\n")
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
