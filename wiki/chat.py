#!/usr/bin/env python3
"""
Wiki 多轮对话 (CLI 版) - Skill 渐进性披露

跟 MikuNotes Flutter 端逻辑一样:
  第 1 轮: 给 LLM manifest (所有视频标题/标签/UP主), 让它说要加载哪些
  第 2 轮: 加载这些视频的 .md, 再让 LLM 基于内容回答

用法:
  python3 wiki_chat.py
  python3 wiki_chat.py --input ./wiki/
  python3 wiki_chat.py --input ./wiki/ --model deepseek-chat
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# LLM 客户端 (复用 summarize.py)
sys.path.insert(0, str(Path(__file__).parent))
from summarize import _llm_call, _get_llm_client  # noqa: E402


WIKI_DIR = Path('./wiki')

# ─────────────────────────────────────────────────────────────────────
# Manifest 加载
# ─────────────────────────────────────────────────────────────────────

def load_manifest(wiki_dir: Path) -> list[dict]:
    """从 wiki/videos/*.md 加载 manifest"""
    videos_dir = wiki_dir / 'videos'
    if not videos_dir.exists():
        videos_dir = wiki_dir
    out = []
    for md in sorted(videos_dir.glob('*.md')):
        v = parse_video_md(md)
        if v.get('bvid', '').startswith('BV'):
            out.append(v)
    return out


def parse_video_md(md_path: Path) -> dict:
    """从 .md 解析元数据 (简化版)"""
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
    }

    m = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
    if m:
        for line in m.group(1).split('\n'):
            line = line.strip()
            if line.startswith('title:'):
                meta['title'] = line[6:].strip().strip('"')
            elif line.startswith('uploader:'):
                meta['uploader'] = line[9:].strip().strip('"')

    tag_block = re.search(r'# 🏷️ 标签\n+(.*?)(?=\n# |\Z)', text, re.DOTALL)
    if tag_block:
        content = tag_block.group(1)
        m = re.search(r'\*\*手动\*\*:\s*(.+)', content)
        if m:
            meta['tags'] = re.findall(r'#(\S+?)`', m.group(1))
        m = re.search(r'\*\*AI 提取\*\*:\s*(.+)', content)
        if m:
            meta['ai_tags'] = re.findall(r'#(\S+?)`', m.group(1))

    return meta


def fmt_duration(sec: int) -> str:
    if sec <= 0:
        return '?'
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f'{h}:{m:02d}:{s:02d}' if h > 0 else f'{m}:{s:02d}'


def build_manifest_prompt(manifest: list[dict]) -> str:
    """生成 manifest 字符串 (注入 system prompt)"""
    if not manifest:
        return '用户尚未导入任何视频, Wiki 暂无内容。'
    buf = [f'## 📚 LLM Wiki Manifest ({len(manifest)} 个视频)\n']
    buf.append('用户问问题时, 如需细节先用 `<need_to_read>BVxxx,BVxxx</need_to_read>` 标记要加载的视频。\n')
    for m in manifest:
        all_tags = ', '.join(m.get('tags', []) + m.get('ai_tags', [])) or '(无标签)'
        dur = fmt_duration(0)  # manifest 里没 duration
        buf.append(f"- {m['bvid']} · {m['title']} · {m['uploader']} · 📌 {all_tags}")
    return '\n'.join(buf)


# ─────────────────────────────────────────────────────────────────────
# 视频内容加载
# ─────────────────────────────────────────────────────────────────────

def load_video_content(wiki_dir: Path, bvid: str) -> str:
    """加载一个视频的完整 .md"""
    videos_dir = wiki_dir / 'videos'
    if not videos_dir.exists():
        videos_dir = wiki_dir
    for md in videos_dir.glob(f'{bvid}_*.md'):
        return md.read_text(encoding='utf-8', errors='ignore')
    return ''


def extract_bvid_requests(llm_response: str) -> list[str]:
    """解析 LLM 输出的 <need_to_read>BVxxx,BVxxx</need_to_read>"""
    m = re.search(r'<need_to_read>([^<]+)</need_to_read>', llm_response)
    if not m:
        return []
    return [s.strip() for s in m.group(1).split(',') if s.strip().startswith('BV')]


def strip_need_to_read(text: str) -> str:
    """移除 need_to_read 标签"""
    return re.sub(r'<need_to_read>[^<]+</need_to_read>', '', text).strip()


# ─────────────────────────────────────────────────────────────────────
# 主循环
# ─────────────────────────────────────────────────────────────────────

def chat_loop(wiki_dir: Path, model: str = 'deepseek-chat'):
    if not wiki_dir.exists():
        print(f'❌ Wiki 目录不存在: {wiki_dir}')
        print('   先跑: python3 wiki_gen.py --input ./downloads/')
        return 1

    print(f'📂 加载 manifest: {wiki_dir}/ ...')
    manifest = load_manifest(wiki_dir)
    if not manifest:
        print('⚠️  Wiki 为空, 先跑 wiki_gen.py')
        return 1
    print(f'✓ {len(manifest)} 个视频\n')

    manifest_str = build_manifest_prompt(manifest)
    system_prompt = f"""你是 MikuNotes Wiki 助手, 能访问用户的视频库 Wiki。

{manifest_str}

## 你的工作方式
1. 收到问题先看 manifest 是否够用
2. 需要细节时输出 `<need_to_read>BVxxx,BVxxx</need_to_read>` 标记要加载的视频
3. 加载的内容会在下一轮提供, 然后你基于内容回答
4. 不要凭 manifest 编造细节

## 回答风格
- 中文
- 用 markdown
- 引用视频用 `[标题](BVxxx)` 格式
"""

    history = []  # [{role, content}]

    print('━' * 60)
    print('  Wiki 多轮对话 (Skill 渐进性披露)')
    print('  输入 "exit" 退出, "list" 列出 manifest')
    print('━' * 60)
    print()

    while True:
        try:
            user_input = input('👤 你: ').strip()
        except (KeyboardInterrupt, EOFError):
            print('\n👋 再见')
            break

        if not user_input:
            continue
        if user_input == 'exit':
            print('👋 再见')
            break
        if user_input == 'list':
            print(f'\n共 {len(manifest)} 个视频:')
            for m in manifest[:20]:
                print(f"  {m['bvid']} · {m['title'][:30]} · {m['uploader']}")
            if len(manifest) > 20:
                print(f'  ... +{len(manifest) - 20} 更多')
            print()
            continue

        print('\n🤖 AI: 思考中...', flush=True)

        try:
            # 第 1 轮: 问 LLM 要加载哪些
            messages = list(history) + [{'role': 'user', 'content': user_input}]
            first = _llm_call(
                client_cfg={'model': model},
                system=system_prompt,
                user='',  # 不直接用 user, 用 messages
                max_tokens=2000,
            ) if False else _chat_call(messages, system_prompt, model)

            # 解析
            bvids = extract_bvid_requests(first)
            if not bvids:
                cleaned = strip_need_to_read(first)
                print(f'   {cleaned}\n')
                history.append({'role': 'user', 'content': user_input})
                history.append({'role': 'assistant', 'content': cleaned})
                continue

            # 第 2 轮: 加载内容
            print(f'   📚 加载 {len(bvids)} 个视频: {", ".join(bvids)}...', flush=True)
            loaded = []
            for b in bvids:
                content = load_video_content(wiki_dir, b)
                if content:
                    loaded.append(f'### {b}\n```markdown\n{content}\n```')
            loaded_section = '\n\n'.join(loaded)

            second_user = f'{user_input}\n\n---\n\n已加载:\n\n{loaded_section}\n\n请基于内容回答。'
            messages2 = list(history) + [
                {'role': 'user', 'content': user_input},
                {'role': 'user', 'content': second_user},  # 多轮 trick: 把内容作为后续 user msg
            ]
            second = _chat_call(messages2, system_prompt, model)
            print(f'   {second}\n')

            history.append({'role': 'user', 'content': user_input})
            history.append({'role': 'assistant', 'content': second})

        except Exception as e:
            print(f'\n❌ 错误: {e}\n')


def _chat_call(messages: list[dict], system_prompt: str, model: str) -> str:
    """调 LLM, 把 messages 列表拼成 user message (因为 summarize.py 的 _llm_call 是单 user)"""
    # 把整个 messages 拼成一个 user 消息
    parts = []
    for m in messages:
        role = '用户' if m['role'] == 'user' else 'AI'
        parts.append(f'{role}: {m["content"]}')
    user_text = '\n\n'.join(parts)
    return _llm_call(
        client_cfg={'model': model},
        system=system_prompt,
        user=user_text,
        max_tokens=2000,
    )


# ─────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Wiki 多轮对话 (CLI)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--input', '-i', default='./wiki', help='wiki 目录')
    parser.add_argument('--model', default='deepseek-chat', help='LLM 模型 (默认 deepseek-chat)')
    args = parser.parse_args()

    return chat_loop(Path(args.input), args.model)


if __name__ == '__main__':
    sys.exit(main())
