#!/usr/bin/env python3
"""
LLM Wiki 生成器 — 从 bilibili-summarizer 的 downloads/ 目录生成 .md 文件

每个视频一个 .md, 含:
  - Frontmatter (元数据)
  - 分P 列表
  - 字幕摘要 (前 30 行)
  - (可选) 总结 (从 .summary.json 读)

用法:
  python3 wiki_gen.py                          # 默认输出到 ./wiki/
  python3 wiki_gen.py --output ./my_wiki/      # 指定输出目录
  python3 wiki_gen.py --downloads ./downloads/ # 指定 downloads 目录
  python3 wiki_gen.py --with-summaries         # 包含 .summary.json 里的总结 (如果有)

输出结构:
  ./wiki/
    ├── index.md
    └── videos/
        ├── BV1xxx_标题.md
        ├── BV2yyy_标题.md
        └── ...
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────────────────────

class VideoPage:
    """一个分P"""
    def __init__(self, page_num: int, srt_path: Path, text: str):
        self.page_num = page_num
        self.srt_path = srt_path
        self.text = text


class VideoWiki:
    """一个视频的 wiki 数据"""
    def __init__(self, bvid: str, title: str, uploader: str, pages: list[VideoPage],
                 duration: int = 0, tags: list[str] = None, ai_tags: list[str] = None,
                 cover: str = '', page_names: list[str] = None,
                 summary: str = '', summary_model: str = ''):
        self.bvid = bvid
        self.title = title
        self.uploader = uploader
        self.duration = duration
        self.tags = tags or []
        self.ai_tags = ai_tags or []
        self.cover = cover
        self.page_names = page_names or []
        self.pages = pages
        self.summary = summary
        self.summary_model = summary_model


# ─────────────────────────────────────────────────────────────────────
# 读取字幕
# ─────────────────────────────────────────────────────────────────────

def parse_srt_time(t: str) -> int:
    """00:01:23,456 → 83 秒"""
    h, m, s_full = t.split(':')
    s = s_full.split(',')[0]
    return int(h) * 3600 + int(m) * 60 + int(s)


def extract_srt_text(srt_path: Path) -> tuple[str, int]:
    """从 .srt 提取纯文本 + 时长 (秒)"""
    if not srt_path.exists():
        return '', 0
    text = srt_path.read_text(encoding='utf-8', errors='ignore')
    # 移除序号和时间戳
    lines = []
    last_end = 0
    for line in text.split('\n'):
        line = line.rstrip()
        if not line:
            continue
        if re.match(r'^\d+$', line):  # 序号
            continue
        if re.match(r'^\d{2}:\d{2}:\d{2}', line):  # 时间戳行
            # 提取结束时间
            m = re.match(r'(\d{2}:\d{2}:\d{2}),\d{3} --> (\d{2}:\d{2}:\d{2}),\d{3}', line)
            if m:
                last_end = parse_srt_time(m.group(2))
            continue
        lines.append(line)
    return ' '.join(lines), last_end


def load_video_from_dir(video_dir: Path) -> Optional[VideoWiki]:
    """从一个视频目录加载 wiki 数据"""
    # 解析目录名: BV1xxxxx
    bvid = video_dir.name
    if not bvid.startswith('BV'):
        return None

    # 读取所有分P
    pages = []
    max_duration = 0
    for srt_path in sorted(video_dir.glob('*_transcript.srt')):
        # P1_transcript.srt → 1
        m = re.match(r'P(\d+)_transcript\.srt', srt_path.name)
        if not m:
            continue
        page_num = int(m.group(1))
        text, duration = extract_srt_text(srt_path)
        pages.append(VideoPage(page_num, srt_path, text))
        max_duration = max(max_duration, duration)

    if not pages:
        return None
    pages.sort(key=lambda p: p.page_num)

    # 读取元数据 (如果有)
    title = bvid
    uploader = ''
    cover = ''
    tags = []
    ai_tags = []
    summary = ''
    summary_model = ''

    meta_path = video_dir / 'meta.json'
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            title = meta.get('title', bvid)
            uploader = meta.get('uploader', '') or meta.get('owner', {}).get('name', '')
            cover = meta.get('cover', '') or meta.get('pic', '')
            duration = meta.get('duration', max_duration)
            page_names = meta.get('page_names', [])
            tags = meta.get('tags', [])
            ai_tags = meta.get('ai_tags', [])
        except Exception:
            pass
    else:
        duration = max_duration
        page_names = []

    # 读取总结 (如果有)
    summary_path = video_dir / 'summary.md'
    if summary_path.exists():
        summary = summary_path.read_text(encoding='utf-8').strip()
        # 尝试从 frontmatter 读 model
        m = re.match(r'^---\n(.*?)\n---', summary, re.DOTALL)
        if m:
            for line in m.group(1).split('\n'):
                if line.startswith('model:'):
                    summary_model = line.split(':', 1)[1].strip()

    return VideoWiki(
        bvid=bvid, title=title, uploader=uploader, duration=duration,
        tags=tags, ai_tags=ai_tags, cover=cover, page_names=page_names,
        pages=pages, summary=summary, summary_model=summary_model,
    )


# ─────────────────────────────────────────────────────────────────────
# 渲染
# ─────────────────────────────────────────────────────────────────────

def slugify(s: str) -> str:
    """标题 → 文件名安全 slug"""
    s = re.sub(r'[\s/\\:*?"<>|]+', '_', s)
    s = re.sub(r'_+', '_', s)
    s = s.strip('_')
    return s[:50]


def fmt_duration(sec: int) -> str:
    if sec <= 0:
        return '未知'
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m}:{s:02d}'


def render_video(v: VideoWiki, with_summaries: bool = True) -> str:
    """渲染单个视频的 .md"""
    buf = []
    # Frontmatter
    buf.append('---')
    buf.append('type: video')
    buf.append(f'bvid: {v.bvid}')
    buf.append(f'title: "{v.title}"')
    if v.uploader:
        buf.append(f'uploader: "{v.uploader}"')
    if v.cover:
        buf.append(f'cover: {v.cover}')
    buf.append(f'duration: {v.duration}')
    buf.append(f'page_count: {len(v.pages)}')
    if v.page_names:
        buf.append(f'page_names: {json.dumps(v.page_names, ensure_ascii=False)}')
    buf.append(f'exported_at: {datetime.now().isoformat()}')
    buf.append('---')
    buf.append('')

    # 标题
    buf.append(f'# {v.title}')
    buf.append('')
    if v.uploader:
        meta_line = f'> UP: **{v.uploader}** · {fmt_duration(v.duration)}'
        if v.page_names:
            meta_line += f' · {len(v.pages)}P'
        buf.append(meta_line)
        buf.append('')

    # 标签
    buf.append('# 🏷️ 标签')
    buf.append('')
    if v.tags or v.ai_tags:
        if v.tags:
            buf.append('**手动**: ' + ' '.join(f'`#{t}`' for t in v.tags))
        if v.ai_tags:
            buf.append('**AI 提取**: ' + ' '.join(f'`#{t}`' for t in v.ai_tags))
    else:
        buf.append('_(无标签)_')
    buf.append('')
    buf.append('---')
    buf.append('')

    # 总结
    if v.summary and with_summaries:
        buf.append('# 📝 AI 总结')
        buf.append('')
        if v.summary_model:
            buf.append(f'**模型**: {v.summary_model}')
            buf.append('')
        # 如果有 frontmatter 去掉
        summary_text = v.summary
        m = re.match(r'^---\n.*?\n---\n', summary_text, re.DOTALL)
        if m:
            summary_text = summary_text[m.end():]
        buf.append(summary_text.strip())
        buf.append('')
        buf.append('---')
        buf.append('')

    # 分P
    buf.append(f'# 📺 分P ({len(v.pages)} 个)')
    buf.append('')
    for p in v.pages:
        page_label = v.page_names[p.page_num - 1] if p.page_num <= len(v.page_names) else f'P{p.page_num}'
        buf.append(f'## P{p.page_num} · {page_label}')
        buf.append('')
        # 字幕前 30 行
        lines = p.text.split('\n') if p.text else []
        snippet = '\n'.join(lines[:30])
        buf.append('**字幕摘要** (前 30 行):')
        buf.append('')
        buf.append('```')
        buf.append(snippet)
        buf.append('```')
        buf.append('')

        # 完整字幕文件位置
        buf.append(f'完整字幕: `{p.srt_path}`')
        buf.append('')
        buf.append('---')
        buf.append('')

    return '\n'.join(buf)


def render_index(videos: list[VideoWiki]) -> str:
    """渲染 index.md"""
    buf = []
    buf.append('# MikuNotes Wiki 索引 (Python 版)')
    buf.append('')
    buf.append(f'> 共 {len(videos)} 个视频 · 最后更新 {datetime.now().isoformat()[:19].replace("T", " ")}')
    buf.append('')
    buf.append('---')
    buf.append('')

    # 按添加时间 (从 bvid 不可推断, 按 title 排序兜底)
    by_uploader: dict[str, list[VideoWiki]] = {}
    for v in videos:
        by_uploader.setdefault(v.uploader or '未知 UP', []).append(v)

    for up, vs in sorted(by_uploader.items(), key=lambda x: -len(x[1])):
        buf.append(f'## 👤 {up} ({len(vs)} 个)')
        buf.append('')
        for v in sorted(vs, key=lambda x: x.title):
            slug = slugify(v.title)
            buf.append(f'- **{v.bvid}** · [{v.title}](videos/{v.bvid}_{slug}.md) — {fmt_duration(v.duration)}')
        buf.append('')

    # 标签统计
    tag_count: dict[str, int] = {}
    for v in videos:
        for t in v.tags + v.ai_tags:
            tag_count[t] = tag_count.get(t, 0) + 1
    if tag_count:
        buf.append('## 🏷️ 标签统计')
        buf.append('')
        for t, c in sorted(tag_count.items(), key=lambda x: -x[1])[:30]:
            buf.append(f'- `#{t}` — {c} 个视频')
        buf.append('')

    return '\n'.join(buf)


# ─────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='从 bilibili-summarizer downloads/ 生成 LLM Wiki .md',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--downloads', default='./downloads',
                        help='downloads 目录 (默认 ./downloads)')
    parser.add_argument('--output', '-o', default='./wiki',
                        help='输出目录 (默认 ./wiki)')
    parser.add_argument('--with-summaries', action='store_true', default=True,
                        help='包含 summary.md (默认开)')
    parser.add_argument('--no-summaries', dest='with_summaries', action='store_false',
                        help='不包含 summary')
    args = parser.parse_args()

    downloads = Path(args.downloads)
    output = Path(args.output)
    if not downloads.exists():
        print(f'❌ downloads 不存在: {downloads}')
        return 1

    output.mkdir(parents=True, exist_ok=True)
    (output / 'videos').mkdir(exist_ok=True)

    # 加载所有视频
    print(f'📂 扫描 {downloads}/ ...')
    videos = []
    for video_dir in sorted(downloads.iterdir()):
        if not video_dir.is_dir():
            continue
        v = load_video_from_dir(video_dir)
        if v is None:
            continue
        videos.append(v)

    print(f'✓ 加载 {len(videos)} 个视频')

    # 写每个视频的 .md
    for v in videos:
        slug = slugify(v.title)
        out = output / 'videos' / f'{v.bvid}_{slug}.md'
        out.write_text(render_video(v, args.with_summaries), encoding='utf-8')
        print(f'  ✓ {out.name}')

    # 写 index
    index = output / 'index.md'
    index.write_text(render_index(videos), encoding='utf-8')
    print(f'\n✓ index: {index}')

    print(f'\n📊 共 {len(videos)} 个视频 .md + 1 index.md')
    print(f'   输出: {output}/')
    return 0


if __name__ == '__main__':
    sys.exit(main())
