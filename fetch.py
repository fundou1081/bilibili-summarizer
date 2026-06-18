#!/usr/bin/env python3
"""
B 站内容批量拉取 (Python 端主入口, 适合 cron 定时任务)

支持的数据源:
  - favorites  收藏夹
  - watch_later 稍后观看
  - followings 关注的 UP 主
  - up          指定 UP 主

增量模式 (--incremental): 跳过已下载 (有字幕的) BVID

用法:
  # 收藏夹 (增量)
  python3 fetch.py favorites --incremental

  # 关注的 UP 主 (增量)
  python3 fetch.py followings --incremental

  # 指定 UP 主
  python3 fetch.py up --mid 12345

  # 所有源 (适合 cron)
  python3 fetch.py all --incremental
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp

# B 站相关
try:
    from bilibili_api import user as bili_user
    from bilibili_api import video as bili_video
    HAS_BILI_API = True
except ImportError:
    HAS_BILI_API = False
    print('⚠️  bilibili-api-python 未安装', file=sys.stderr)

# 字幕下载复用
sys.path.insert(0, str(Path(__file__).parent))
from bilibili_cc import download_subtitles_async  # noqa: E402


DOWNLOADS_DIR = Path('downloads')
COOKIE_FILE = Path('.credential.json')

# ─────────────────────────────────────────────────────────────────────
# 凭据
# ─────────────────────────────────────────────────────────────────────

def load_credential() -> dict:
    """从 .credential.json 读 SESSDATA (登录用)"""
    if not COOKIE_FILE.exists():
        return {}
    try:
        return json.loads(COOKIE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_credential(cred: dict):
    COOKIE_FILE.write_text(json.dumps(cred, ensure_ascii=False, indent=2), encoding='utf-8')


# ─────────────────────────────────────────────────────────────────────
# 增量检查
# ─────────────────────────────────────────────────────────────────────

def already_downloaded(bvid: str) -> bool:
    bvid_dir = DOWNLOADS_DIR / bvid
    if not bvid_dir.exists():
        return False
    return any(bvid_dir.glob('*_transcript.srt'))


# ─────────────────────────────────────────────────────────────────────
# 元数据
# ─────────────────────────────────────────────────────────────────────

async def fetch_video_meta_async(bvid: str) -> dict:
    if not HAS_BILI_API:
        return {}
    try:
        v = bili_video.Video(bvid=bvid)
        info = await v.get_info()
        return {
            'bvid': bvid,
            'aid': info.get('aid'),
            'title': info.get('title', ''),
            'cover': info.get('pic', ''),
            'uploader': info.get('owner', {}).get('name', ''),
            'up_mid': info.get('owner', {}).get('mid'),
            'duration': info.get('duration', 0),
            'page_count': len(info.get('pages', [])),
            'page_names': [p.get('part', '') for p in info.get('pages', [])],
            'desc': info.get('desc', ''),
            'pubdate': info.get('pubdate', 0),
        }
    except Exception as e:
        print(f'  ⚠️  meta {bvid}: {e}')
        return {}


def save_meta(bvid: str, meta: dict):
    if not meta:
        return
    meta_path = DOWNLOADS_DIR / bvid / 'meta.json'
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')


# ─────────────────────────────────────────────────────────────────────
# 视频列表获取
# ─────────────────────────────────────────────────────────────────────

async def get_favorite_folders(mid: int) -> list[dict]:
    """拿用户的所有收藏夹 (含 id + title)"""
    if not HAS_BILI_API:
        return []
    try:
        u = bili_user.User(mid=mid, credential=load_credential())
        # get_fav_folder_info_and_videos? or use favorite API
        favs = await u.get_favorite_folder()
        return favs
    except Exception as e:
        print(f'  ⚠️  收藏夹: {e}')
        return []


async def get_favorite_videos(media_id: int, page_size: int = 20) -> list[str]:
    """拿一个收藏夹的所有视频 bvid"""
    if not HAS_BILI_API:
        return []
    bvids = []
    page = 1
    while True:
        try:
            # bilibili-api-python 的 API
            from bilibili_api.favorite_list import FavoriteListVideo
            f = FavoriteListVideo(media_id=media_id, credential=load_credential())
            info = await f.get_page(page, page_size)
            items = info.get('medias') or info.get('data', {}).get('medias', [])
            if not items:
                break
            for item in items:
                if 'bvid' in item:
                    bvids.append(item['bvid'])
            if len(items) < page_size:
                break
            page += 1
            time.sleep(0.3)
        except Exception as e:
            print(f'  ⚠️  收藏夹 {media_id} page {page}: {e}')
            break
    return bvids


async def get_watch_later_videos() -> list[str]:
    """稍后观看列表"""
    if not HAS_BILI_API:
        return []
    try:
        # bilibili-api-python 没有 watch_later API, 用 web API
        # 这里用 HTTP 直接调
        cred = load_credential()
        sessdata = cred.get('SESSDATA') or cred.get('sessdata', '')
        url = 'https://api.bilibili.com/x/v2/history/toview'
        cookies = {'SESSDATA': sessdata}
        async with aiohttp.ClientSession(cookies=cookies) as s:
            async with s.get(url) as r:
                data = await r.json()
        items = data.get('data', {}).get('list', [])
        return [it['bvid'] for it in items if 'bvid' in it]
    except Exception as e:
        print(f'  ⚠️  稍后观看: {e}')
        return []


async def get_followings(mid: int) -> list[dict]:
    """拿关注的所有 UP 主 [{mid, name, ...}]"""
    if not HAS_BILI_API:
        return []
    try:
        u = bili_user.User(mid=mid, credential=load_credential())
        page = 1
        ups = []
        while True:
            rels = await u.get_followings(pn=page, ps=50)
            if not rels:
                break
            ups.extend(rels)
            if len(rels) < 50:
                break
            page += 1
            time.sleep(0.3)
        return ups
    except Exception as e:
        print(f'  ⚠️  followings: {e}')
        return []


async def get_up_videos(mid: int, max_pages: int = 5) -> list[str]:
    """拿指定 UP 主的所有投稿 bvid (限制页数避免太多)"""
    if not HAS_BILI_API:
        return []
    try:
        u = bili_user.User(mid=mid, credential=load_credential())
        videos = await u.get_videos(pn=1, ps=30)
        # bilibili-api-python: videos 是 dict {'list': [...], 'page': ..., 'total': ...}
        items = videos.get('list', {}).get('vlist', []) if isinstance(videos, dict) else []
        if not items and isinstance(videos, dict):
            items = videos.get('list', [])
        return [it.get('bvid') for it in items if it.get('bvid')]
    except Exception as e:
        print(f'  ⚠️  up {mid}: {e}')
        return []


# ─────────────────────────────────────────────────────────────────────
# 编排
# ─────────────────────────────────────────────────────────────────────

async def process_one(bvid: str) -> bool:
    """处理一个 BVID: 下载字幕 + 写元数据"""
    try:
        # 1. 下载字幕 (需要传完整 URL)
        url = f'https://www.bilibili.com/video/{bvid}'
        out_dir = str(DOWNLOADS_DIR / bvid)
        count = await download_subtitles_async(
            url=url, output_dir=out_dir, p_start=0, p_end=0, auto_convert=True
        )
        # 2. 写元数据
        meta = await fetch_video_meta_async(bvid)
        save_meta(bvid, meta)
        return count == 0  # download_subtitles_async 返回 0=成功
    except Exception as e:
        print(f'  ✗ {bvid}: {e}')
        return False


async def run_source(name: str, bvids: list[str], incremental: bool) -> list[str]:
    """处理一个数据源的所有 BVID"""
    seen = set()
    unique = [b for b in bvids if b and not (b in seen or seen.add(b))]
    print(f'  ✓ 去重: {len(unique)} 个唯一 BVID')

    if incremental:
        to_process = [b for b in unique if not already_downloaded(b)]
        skipped = len(unique) - len(to_process)
        print(f'  ⏭️  跳过已下载: {skipped}')
    else:
        to_process = unique
        print(f'  📥 全量处理: {len(to_process)}')

    new_bvids = []
    for i, b in enumerate(to_process, 1):
        print(f'  [{i}/{len(to_process)}] {b}...', end=' ', flush=True)
        ok = await process_one(b)
        if ok:
            new_bvids.append(b)
            print('✓')
        else:
            print('✗')
        time.sleep(0.5)  # 礼貌延迟
    return new_bvids


# ─────────────────────────────────────────────────────────────────────
# 命令实现
# ─────────────────────────────────────────────────────────────────────

async def cmd_favorites(mid: int, incremental: bool) -> list[str]:
    print('📂 收藏夹...')
    folders = await get_favorite_folders(mid)
    print(f'  ✓ {len(folders)} 个收藏夹')

    all_bvids = []
    for f in folders:
        fid = f.get('id')
        fname = f.get('title', f'folder_{fid}')
        print(f'  📁 {fname} (id={fid})')
        bvids = await get_favorite_videos(fid)
        all_bvids.extend(bvids)

    return await run_source('favorites', all_bvids, incremental)


async def cmd_watch_later(incremental: bool) -> list[str]:
    print('⏰ 稍后观看...')
    bvids = await get_watch_later_videos()
    if not bvids:
        print('  ⚠️  watch_later 需要登录态 (.credential.json 里有 SESSDATA)')
    return await run_source('watch_later', bvids, incremental)


async def cmd_followings(mid: int, incremental: bool) -> list[str]:
    print('👥 关注的 UP 主...')
    ups = await get_followings(mid)
    print(f'  ✓ 关注 {len(ups)} 个 UP')

    all_bvids = []
    for up in ups:
        up_mid = up.get('mid')
        up_name = up.get('uname', f'mid_{up_mid}')
        bvids = await get_up_videos(up_mid, max_pages=2)  # 每个 UP 只拉前 2 页
        print(f'  📺 {up_name}: {len(bvids)} 个')
        all_bvids.extend(bvids)
    return await run_source('followings', all_bvids, incremental)


async def cmd_up(mid: int, incremental: bool) -> list[str]:
    print(f'👤 UP 主 mid={mid}...')
    bvids = await get_up_videos(mid)
    return await run_source(f'up:{mid}', bvids, incremental)


async def cmd_all(mid: int, incremental: bool) -> list[str]:
    new = []
    new += await cmd_favorites(mid, incremental)
    new += await cmd_watch_later(incremental)
    new += await cmd_followings(mid, incremental)
    return new


# ─────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='B 站批量拉取 (收藏夹/稍后观看/followings/UP主)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--mid', type=int, default=0,
                        help='你的 B 站 UID (用于收藏夹/followings)')
    parser.add_argument('--incremental', action='store_true',
                        help='跳过已下载 (有字幕的) BVID')

    sub = parser.add_subparsers(dest='cmd', required=True, help='数据源')
    sub.add_parser('favorites', help='收藏夹')
    sub.add_parser('watch_later', help='稍后观看')
    sub.add_parser('followings', help='关注的 UP 主')
    sub.add_parser('all', help='所有源 (收藏夹+稍后观看+followings)')
    up_p = sub.add_parser('up', help='指定 UP 主')
    up_p.add_argument('--mid', type=int, required=True, help='UP 主 mid')

    args = parser.parse_args()
    DOWNLOADS_DIR.mkdir(exist_ok=True)

    if not HAS_BILI_API:
        print('❌ 请先安装: pip install bilibili-api-python aiohttp')
        return 1

    print(f'📂 downloads: {DOWNLOADS_DIR.resolve()}\n')

    try:
        if args.cmd == 'favorites':
            new = asyncio.run(cmd_favorites(args.mid, args.incremental))
        elif args.cmd == 'watch_later':
            new = asyncio.run(cmd_watch_later(args.incremental))
        elif args.cmd == 'followings':
            new = asyncio.run(cmd_followings(args.mid, args.incremental))
        elif args.cmd == 'up':
            new = asyncio.run(cmd_up(args.mid, args.incremental))
        elif args.cmd == 'all':
            new = asyncio.run(cmd_all(args.mid, args.incremental))
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        print('\n⏹️  中断')
        return 130

    print(f'\n✅ 完成: 新增 {len(new)} 个视频')
    return 0


if __name__ == '__main__':
    sys.exit(main())
