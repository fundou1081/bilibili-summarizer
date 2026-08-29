#!/usr/bin/env python3
"""
图可视化 — 视频/标签/UP主 关联图 (Python 版)
用 networkx + matplotlib 渲染

输入: wiki/ 目录 (由 wiki_gen.py 生成)
输出: PNG / HTML / 交互式 HTML (pyvis)

用法:
  python3 wiki_graph.py --input ./wiki/ --output graph.png
  python3 wiki_graph.py --input ./wiki/ --output graph.html --format html  # 交互式
  python3 wiki_graph.py --input ./wiki/ --tag 机器学习                     # 只看某 tag
  python3 wiki_graph.py --input ./wiki/ --up 李宏毅                       # 只看某 UP 主
  python3 wiki_graph.py --input ./wiki/ --layout spring                    # spring/kamada_kawai/circular
"""

import argparse
import json
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use('Agg')  # 无头模式
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.font_manager as fm
import networkx as nx

# 设置中文字体 (macOS)
for font_path in [
    '/System/Library/Fonts/PingFang.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
    '/System/Library/Fonts/STHeiti Light.ttc',
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
]:
    try:
        fm.fontManager.addfont(font_path)
    except Exception:
        pass
plt.rcParams['font.sans-serif'] = ['PingFang SC', 'STHeiti', 'Hiragino Sans GB', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False


# ─────────────────────────────────────────────────────────────────────
# 解析 wiki 目录
# ─────────────────────────────────────────────────────────────────────

def parse_video_md(md_path: Path) -> dict:
    """从一个视频的 .md 解析元数据"""
    try:
        text = md_path.read_text(encoding='utf-8')
    except Exception:
        return {}

    meta = {
        'bvid': md_path.stem.split('_')[0],
        'title': '',
        'uploader': '',
        'tags': [],
        'ai_tags': [],
        'path': str(md_path),
    }

    # Frontmatter
    m = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
    if m:
        for line in m.group(1).split('\n'):
            line = line.strip()
            if line.startswith('title:'):
                meta['title'] = line[6:].strip().strip('"')
            elif line.startswith('uploader:'):
                meta['uploader'] = line[9:].strip().strip('"')

    # 标签块
    tag_block = re.search(r'# 🏷️ 标签\n+(.*?)(?=\n# |\Z)', text, re.DOTALL)
    if tag_block:
        content = tag_block.group(1)
        # 手动: #tag1 #tag2
        m = re.search(r'\*\*手动\*\*:\s*(.+)', content)
        if m:
            meta['tags'] = re.findall(r'#(\S+?)`', m.group(1))
        # AI 提取: #tag1 #tag2
        m = re.search(r'\*\*AI 提取\*\*:\s*(.+)', content)
        if m:
            meta['ai_tags'] = re.findall(r'#(\S+?)`', m.group(1))

    return meta


def load_videos(wiki_dir: Path) -> list[dict]:
    """从 wiki/videos/ 加载所有视频"""
    videos_dir = wiki_dir / 'videos'
    if not videos_dir.exists():
        # 兜底: 直接从 wiki 根目录读
        videos_dir = wiki_dir
    out = []
    for md in sorted(videos_dir.glob('*.md')):
        v = parse_video_md(md)
        if v.get('bvid', '').startswith('BV'):
            out.append(v)
    return out


# ─────────────────────────────────────────────────────────────────────
# 构建图
# ─────────────────────────────────────────────────────────────────────

NODE_VIDEO = 'video'
NODE_TAG = 'tag'
NODE_UP = 'up'

COLOR_VIDEO = '#4A90E2'   # 蓝
COLOR_TAG = '#F5A623'     # 橙
COLOR_UP = '#9013FE'      # 紫

def build_graph(videos: list[dict], tag_filter: set = None, up_filter: set = None) -> nx.Graph:
    """构建关联图"""
    g = nx.Graph()

    for v in videos:
        # 标签/UP 筛选
        if tag_filter:
            all_tags = set(v.get('tags', [])) | set(v.get('ai_tags', []))
            if not (all_tags & tag_filter):
                continue
        if up_filter:
            if v.get('uploader', '') not in up_filter:
                continue

        bvid = v['bvid']
        title = v.get('title') or bvid
        uploader = v.get('uploader', '')

        # 视频节点
        g.add_node(bvid, type=NODE_VIDEO, label=title[:20], uploader=uploader)

        # 标签节点
        for t in set(v.get('tags', [])) | set(v.get('ai_tags', [])):
            tag_id = f'tag:{t}'
            if tag_id not in g:
                g.add_node(tag_id, type=NODE_TAG, label=f'#{t}')
            g.add_edge(bvid, tag_id, weight=1.5)

        # UP 主节点
        if uploader:
            up_id = f'up:{uploader}'
            if up_id not in g:
                g.add_node(up_id, type=NODE_UP, label=uploader)
            g.add_edge(bvid, up_id, weight=2.0)

    return g


# ─────────────────────────────────────────────────────────────────────
# 渲染
# ─────────────────────────────────────────────────────────────────────

def render_matplotlib(g: nx.Graph, output: Path, layout: str = 'spring'):
    """渲染为 PNG"""
    if g.number_of_nodes() == 0:
        print('⚠️  图为空')
        return

    # 选布局
    if layout == 'kamada_kawai' and g.number_of_nodes() <= 100:
        try:
            pos = nx.kamada_kawai_layout(g)
        except Exception:
            pos = nx.spring_layout(g, k=2.0, iterations=100)
    elif layout == 'circular':
        pos = nx.circular_layout(g)
    else:
        pos = nx.spring_layout(g, k=2.5, iterations=100, seed=42)

    fig, ax = plt.subplots(figsize=(20, 16))

    # 按类型分节点
    video_nodes = [n for n, d in g.nodes(data=True) if d.get('type') == NODE_VIDEO]
    tag_nodes = [n for n, d in g.nodes(data=True) if d.get('type') == NODE_TAG]
    up_nodes = [n for n, d in g.nodes(data=True) if d.get('type') == NODE_UP]

    # 画边
    nx.draw_networkx_edges(g, pos, alpha=0.3, width=0.5, ax=ax, edge_color='gray')

    # 画节点
    nx.draw_networkx_nodes(g, pos, nodelist=video_nodes, node_color=COLOR_VIDEO,
                           node_size=200, alpha=0.9, ax=ax, edgecolors='white', linewidths=0.5)
    nx.draw_networkx_nodes(g, pos, nodelist=tag_nodes, node_color=COLOR_TAG,
                           node_size=150, alpha=0.9, ax=ax, edgecolors='white', linewidths=0.5)
    nx.draw_networkx_nodes(g, pos, nodelist=up_nodes, node_color=COLOR_UP,
                           node_size=300, alpha=0.9, ax=ax, edgecolors='white', linewidths=0.5)

    # 标签 (只画 UP 主 + 标签, 视频节点不画避免太挤)
    labels = {}
    for n in set(tag_nodes) | set(up_nodes):
        d = g.nodes[n]
        labels[n] = d.get('label', n)
    nx.draw_networkx_labels(g, pos, labels=labels, font_size=8, ax=ax,
                            font_weight='bold' if g.nodes[n].get('type') == NODE_UP else 'normal')

    # 图例
    legend = [
        mpatches.Patch(color=COLOR_VIDEO, label=f'视频 ({len(video_nodes)})'),
        mpatches.Patch(color=COLOR_TAG, label=f'标签 ({len(tag_nodes)})'),
        mpatches.Patch(color=COLOR_UP, label=f'UP 主 ({len(up_nodes)})'),
    ]
    ax.legend(handles=legend, loc='upper right', fontsize=12)

    ax.set_title(f'LLM Wiki 关联图 · {g.number_of_nodes()} 节点 / {g.number_of_edges()} 边',
                 fontsize=16, fontweight='bold')
    ax.axis('off')
    plt.tight_layout()
    plt.savefig(output, dpi=120, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f'✓ PNG: {output}')


def render_pyvis(g: nx.Graph, output: Path):
    """渲染为交互式 HTML (用 pyvis)"""
    try:
        from pyvis.network import Network
    except ImportError:
        print('❌ 需要安装 pyvis: pip install pyvis')
        return

    if g.number_of_nodes() == 0:
        print('⚠️  图为空')
        return

    net = Network(height='900px', width='100%', bgcolor='white', font_color='#333')

    # 颜色映射
    color_map = {NODE_VIDEO: COLOR_VIDEO, NODE_TAG: COLOR_TAG, NODE_UP: COLOR_UP}
    size_map = {NODE_VIDEO: 15, NODE_TAG: 10, NODE_UP: 25}

    for n, d in g.nodes(data=True):
        net.add_node(n, label=d.get('label', n), color=color_map.get(d.get('type'), '#888'),
                     size=size_map.get(d.get('type'), 10), title=n)

    for u, v in g.edges():
        net.add_edge(u, v)

    net.show_buttons(filter_=['physics'])
    net.save_graph(str(output))
    print(f'✓ HTML: {output}')


# ─────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='视频/标签/UP主 关联图 (Python 版)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--input', '-i', default='./wiki',
                        help='wiki 目录 (默认 ./wiki)')
    parser.add_argument('--output', '-o', default='graph.png',
                        help='输出文件 (默认 graph.png)')
    parser.add_argument('--format', choices=['png', 'html'], default='png',
                        help='输出格式 (默认 png, html 需 pyvis)')
    parser.add_argument('--layout', choices=['spring', 'kamada_kawai', 'circular'],
                        default='spring', help='布局算法 (默认 spring)')
    parser.add_argument('--tag', nargs='+', default=None, help='只显示这些 tag 的视频')
    parser.add_argument('--up', nargs='+', default=None, help='只显示这些 UP 主')
    args = parser.parse_args()

    wiki = Path(args.input)
    if not wiki.exists():
        print(f'❌ 目录不存在: {wiki}')
        return 1

    print(f'📂 读取 {wiki}/ ...')
    videos = load_videos(wiki)
    print(f'✓ 加载 {len(videos)} 个视频')

    tag_filter = set(args.tag) if args.tag else None
    up_filter = set(args.up) if args.up else None
    g = build_graph(videos, tag_filter, up_filter)
    print(f'✓ 图: {g.number_of_nodes()} 节点 / {g.number_of_edges()} 边')

    output = Path(args.output)
    if args.format == 'png':
        render_matplotlib(g, output, args.layout)
    else:
        render_pyvis(g, output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
