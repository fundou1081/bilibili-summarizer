#!/usr/bin/env python3
"""
B站视频内容总结工具 - 支持单个/批量视频

用法:
  # 单个视频
  python3 summarize.py <URL>
  python3 summarize.py <URL> --text-only

  # 批量：从文件读取 URL 列表
  python3 summarize.py --batch urls.txt

  # 批量：命令行传入多个 URL
  python3 summarize.py --urls <URL1> <URL2> <URL3>

  # 批量 + 生成综合对比总结
  python3 summarize.py --batch urls.txt --compare
"""

import sys
import os
import re
import json
import asyncio
import argparse
import urllib.request
import time

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_url(url: str) -> str:
    """解析 b23.tv 短链接"""
    if "b23.tv" in url:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            req.method = "HEAD"
            with urllib.request.urlopen(req, timeout=10) as r:
                loc = r.getheader("Location", "")
                if loc:
                    return loc
        except Exception:
            pass
        req2 = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=10) as r:
            url = r.url
    return url


# ─── 字幕下载 ─────────────────────────────────────────────────────

async def download_subs(url: str, lang_pref: str = "zh") -> tuple[str, str, list]:
    """下载字幕，返回 (srt_path, title, [分P信息])"""
    from bilibili_api import video, Credential

    cred_file = os.path.join(PROJECT_DIR, ".credential.json")
    if not os.path.exists(cred_file):
        raise RuntimeError("请先运行 bilibili_cc.py --login 登录 B站")

    with open(cred_file) as f:
        cred_data = json.load(f)

    credential = Credential(
        sessdata=cred_data.get("sessdata"),
        bili_jct=cred_data.get("bili_jct"),
        dedeuserid=cred_data.get("dedeuserid"),
        ac_time_value=cred_data.get("ac_time_value"),
    )

    bvid = re.search(r'BV[A-Za-z0-9]+', url).group(0)
    v = video.Video(bvid=bvid, credential=credential)
    info = await v.get_info()
    title = info.get("title", "unknown")
    pages = info.get("pages", [])
    cid = pages[0]["cid"]

    subtitle_info = await v.get_subtitle(cid)
    subtitles = subtitle_info.get("subtitles", [])
    if not subtitles:
        raise RuntimeError(f"[{title}] 没有字幕")

    # 优先中文字幕
    target = None
    for sub in subtitles:
        lan = sub.get("lan", "")
        lan_doc = sub.get("lan_doc", "")
        if lang_pref in lan.lower() or "中文" in lan_doc or "zh" in lan.lower():
            target = sub
            break
    if not target:
        target = subtitles[0]

    subtitle_url = target.get("subtitle_url", "")
    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url
    lan_name = target.get("lan_doc", target.get("lan", "unknown"))

    download_dir = os.path.join(PROJECT_DIR, "downloads", bvid)
    os.makedirs(download_dir, exist_ok=True)

    req = urllib.request.Request(subtitle_url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read())

    srt_path = os.path.join(download_dir, "transcript.srt")
    _json_to_srt(raw, srt_path)

    return srt_path, title, pages


def _json_to_srt(data: dict, output_path: str):
    body = data.get("body", [])
    with open(output_path, "w", encoding="utf-8") as out:
        for i, item in enumerate(body, 1):
            def ts(raw):
                s = float(raw)
                h, m = int(s//3600), int((s%3600)//60)
                sec, ms = int(s%60), int((s-int(s))*1000)
                return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"
            out.write(f"{i}\n{ts(item['from'])} --> {ts(item['to'])}\n")
            content = item['content'].replace(chr(10), chr(92) + 'N')
            out.write(f"{content}\n\n")


# ─── 文本提取 ─────────────────────────────────────────────────────

def extract_text(srt_path: str) -> str:
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()
    lines = []
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.isdigit() or "-->" in line:
            continue
        lines.append(line.replace("\\N", ""))
    return "\n".join(lines)


# ─── LLM 总结 ─────────────────────────────────────────────────────

SUMMARY_PROMPT = """你是B站视频内容总结助手。请严格按照以下格式输出结构化总结：

## 📺 视频概述
一句话概括视频主题。

## 🧠 核心概念/名词解释
用表格列出视频中出现的核心概念、术语、专有名词，并给出简洁解释。

## 💡 有价值的观点
列举视频中独特、有启发性的观点（3-5条），每条引用视频中的具体论据。

## 🔑 最重要的观点
提炼视频最核心的1-2个论点，说明为什么这是关键。

## 📐 行文逻辑
用流程图或层级结构展示视频的论证逻辑。

## ❓ 提问-回答
针对视频核心议题，设计3-5个关键问答（Q&A格式）。

要求:
- 使用 Markdown 格式
- 概念解释简洁准确
- 观点引用视频原话
- 板块间用 --- 分隔"""

# ─── 评论分析模板 ────────────────────────────────────────────

COMMENT_COMMUNITY = """你是社区洞察分析师。给定一个B站视频的评论数据，请从以下维度提取信息：

### 1. ❓ 常见问题 (3-5个)
观众反复提到的疑问、困惑

### 2. 📝 补充信息 (3-5个)
观众补充的上下文、额外数据、延伸知识点

### 3. ⚠️ 纠正与争议 (3-5个)
观众指出的错误、争议观点、不同意见

### 4. 😊 整体情感
正面/中性/负面比例，以及典型反馈

### 5. 🌟 最有价值评论 (Top 3)
最值得看的评论全文

视频: {title} (共{total}条评论, 采样{taken}条)\n\n{text}"""

COMMENT_TECH = """你是技术审核专家。请从视频评论中提取技术相关的反馈：

### 1. 🔧 代码/技术错误
观众指出的代码bug、技术错误、配置问题

### 2. 📚 补充技术细节
观众补充的实现细节、替代方案、延伸资源

### 3. ⚡ 性能/优化建议
观众提出的性能优化建议

### 4. 🐛 兼容性问题
平台/版本兼容性反馈

### 5. ✅ 验证反馈
观众验证过可用的方案

视频: {title} (共{total}条评论)\n\n{text}"""

COMMENT_SENTIMENT = """你是舆情分析师。请从评论中分析观众对视频的反应：

### 1. 📊 整体情感分布
正面/中性/负面比例

### 2. 😡 争议焦点
最具争议的话题，不同立场的观点

### 3. 😍 高赞观点
获赞最多的观点和评论

### 4. 🤔 未满足的需求
观众想要但视频未提供的内容

### 5. 💬 典型评论 (各情感类型1条)
最能代表正面/负面/中立态度的评论

视频: {title} (共{total}条评论)\n\n{text}"""

# ─── 对比分析 ──────────────────────────────────────────────

COMPARE_PROMPT = """你是B站视频对比分析助手。以下是多个相关视频的总结，请生成一份综合对比分析：

## 🔗 视频对比总览
用表格列出各视频的标题、核心观点、立场倾向。

## 🎯 共同主题
提炼这些视频的共同讨论焦点。

## ⚖️ 观点分歧
如果不同视频存在观点冲突或互补，详细说明。

## 🧩 知识整合
将这些视频的知识点整合成一个系统性的理解框架。

## 📋 推荐阅读顺序
按从基础到深入的顺序排列观看建议。

要求: Markdown 格式，客观中立，引用各视频的具体内容。"""


def _get_llm_client():
    """返回 (url, api_key, model) 三元组 - 使用 Anthropic 格式"""
    # 优先: DeepSeek
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if api_key:
        return {
            "api": "openai",
            "url": "https://api.deepseek.com/v1/chat/completions",
            "key": api_key,
            "model": "deepseek-chat",
            "is_anthropic": False,
        }

    # 其次: MiniMax (Anthropic 格式)
    api_key = os.environ.get("MINIMAX_API_KEY", "")
    if api_key:
        return {
            "api": "anthropic",
            "url": "https://api.minimaxi.com/anthropic/v1/messages",
            "key": api_key,
            "model": os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7"),
            "is_anthropic": True,
        }

    raise RuntimeError(
        "未找到 LLM API Key。请设置 DEEPSEEK_API_KEY 或 MINIMAX_API_KEY 环境变量"
    )


def _llm_call(client_cfg: dict, system: str, user: str, max_tokens: int = 2000) -> str:
    """统一 LLM 调用, 走 Anthropic 或 OpenAI 格式"""
    import urllib.request

    if client_cfg["is_anthropic"]:
        # Anthropic 格式 + 关闭 thinking
        data = json.dumps({
            "model": client_cfg["model"],
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "enabled", "budget_tokens": 0},  # 关 thinking
        }).encode()
        req = urllib.request.Request(
            client_cfg["url"],
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": client_cfg["key"],
                "anthropic-version": "2023-06-01",
                "Authorization": f"Bearer {client_cfg['key']}",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())
        # 提取 text content (忽略 thinking)
        text_parts = []
        for c in result.get("content", []):
            if c.get("type") == "text":
                text_parts.append(c.get("text", ""))
        return "\n".join(text_parts).strip()
    else:
        # OpenAI 格式
        data = json.dumps({
            "model": client_cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }).encode()
        req = urllib.request.Request(
            client_cfg["url"],
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {client_cfg['key']}",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())
        return result["choices"][0]["message"]["content"]


def summarize_one(transcript: str, title: str) -> str:
    """总结单个视频"""
    cfg = _get_llm_client()

    max_chars = 12000
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars] + "\n\n... (内容过长，已截断)"

    return _llm_call(
        cfg,
        system=SUMMARY_PROMPT,
        user=f"视频标题: {title}\n\n字幕内容:\n{transcript}",
        max_tokens=4096,
    )


def compare_videos(summaries: list[tuple[str, str]]) -> str:
    """对比分析多个视频"""
    cfg = _get_llm_client()

    combined = ""
    for i, (title, summary) in enumerate(summaries, 1):
        combined += f"\n## 视频 {i}: {title}\n{summary[:3000]}\n"

    return _llm_call(
        cfg,
        system=COMPARE_PROMPT,
        user=combined,
        max_tokens=3000,
    )


# ─── 批量处理 ─────────────────────────────────────────────────────

async def process_single(url: str, index: int, total: int, text_only: bool = False,
                         lang: str = "zh") -> dict:
    """处理单个视频，返回结果字典"""
    url = resolve_url(url)
    progress = f"[{index}/{total}]"

    try:
        print(f"\n{'='*60}")
        print(f"{progress} {url}")
        srt_path, title, pages = await download_subs(url, lang)

        text = extract_text(srt_path)
        txt_path = srt_path.replace(".srt", ".txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"{progress} 📝 {title} — {len(text)} 字")

        result = {
            "url": url, "title": title, "srt_path": srt_path,
            "text": text, "summary": "", "error": None
        }

        if not text_only:
            print(f"{progress} 🤖 调用 LLM 总结中...")
            summary = summarize_one(text, title)
            summary_path = srt_path.replace(".srt", "_summary.md")
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(summary)
            result["summary"] = summary
            result["summary_path"] = summary_path
            print(f"{progress} ✅ 总结已保存: {summary_path}")

        return result

    except Exception as e:
        print(f"{progress} ❌ 失败: {e}")
        return {"url": url, "title": "", "error": str(e)}


async def process_batch(urls: list[str], text_only: bool = False, lang: str = "zh",
                        compare: bool = False):
    """批量处理"""
    total = len(urls)
    print(f"\n🚀 批量处理 {total} 个视频\n")

    results = []
    successes = []
    for i, url in enumerate(urls, 1):
        url = url.strip()
        if not url or url.startswith("#"):
            continue
        r = await process_single(url, i, total, text_only, lang)
        results.append(r)
        if not r["error"]:
            successes.append(r)

        # 避免 API 限流
        if i < total and not text_only:
            time.sleep(1)

    # 打印批量总结
    print(f"\n{'='*60}")
    print(f"📊 批量处理完成: {len(successes)}/{total} 成功")

    if successes:
        print("\n--- 处理结果 ---")
        for r in successes:
            status = "✅" if r.get("summary") else "📝"
            print(f"  {status} {r['title'][:50]}")

    # 对比分析
    if compare and len(successes) >= 2:
        summary_pairs = [(r["title"], r.get("summary", "")) for r in successes if r.get("summary")]
        if len(summary_pairs) >= 2:
            print(f"\n🔗 生成综合对比分析...")
            compare_text = compare_videos(summary_pairs)
            compare_path = os.path.join(PROJECT_DIR, "downloads", "compare_summary.md")
            with open(compare_path, "w", encoding="utf-8") as f:
                f.write(compare_text)
            print(f"✅ 对比分析已保存: {compare_path}")
            print(f"\n{compare_text[:500]}...")

    # 失败列表
    failures = [r for r in results if r["error"]]
    if failures:
        print(f"\n⚠️  {len(failures)} 个失败:")
        for r in failures:
            print(f"  ❌ {r['url'][:60]} — {r['error']}")

    # 保存索引
    index_path = os.path.join(PROJECT_DIR, "downloads", "batch_index.json")
    index_data = [{
        "title": r["title"], "url": r["url"],
        "srt_path": r.get("srt_path", ""),
        "summary_path": r.get("summary_path", ""),
        "error": r.get("error"),
    } for r in results]
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    print(f"\n📋 批量索引: {index_path}")

    return results


# ─── CLI ───────────────────────────────────────────────────────────

async def main_async(args):
    # 批量模式
    if args.batch or args.urls:
        if args.batch:
            with open(args.batch) as f:
                urls = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            print(f"📂 从文件读取 {len(urls)} 个 URL: {args.batch}")
        else:
            urls = args.urls

        await process_batch(urls, args.text_only, args.lang, args.compare)
        return

    # 单视频模式
    url = resolve_url(args.url)
    print("=" * 60)
    srt_path, title, pages = await download_subs(url, args.lang)

    text = extract_text(srt_path)
    txt_path = srt_path.replace(".srt", ".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"📝 提取文本: {txt_path} ({len(text)} 字)")

    if args.text_only:
        print("\n" + "=" * 60)
        preview = text[:3000]
        print(preview)
        if len(text) > 3000:
            print(f"\n... (完整文本见 {txt_path})")
        return

    summary = summarize_one(text, title)
    summary_path = srt_path.replace(".srt", "_summary.md")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary)

    print(f"✅ 总结已保存: {summary_path}")
    print("=" * 60)
    print(summary)


def main():
    parser = argparse.ArgumentParser(
        description="B站视频内容总结工具 — 支持单视频/批量处理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 单个视频
  python3 summarize.py https://www.bilibili.com/video/BVxxxxxx

  # 批量：从文件读取
  python3 summarize.py --batch urls.txt

  # 批量：命令行传入
  python3 summarize.py --urls https://b23.tv/xxx https://www.bilibili.com/video/BVyyy

  # 批量 + 对比分析
  python3 summarize.py --batch urls.txt --compare

  # URL 文件格式 (urls.txt) — 一行一个链接:
  https://www.bilibili.com/video/BV1xx...
  https://b23.tv/xxxxx
  # 注释行以 # 开头
        """
    )
    parser.add_argument("url", nargs="?", default="", help="B站视频链接 (单视频模式)")
    parser.add_argument("--batch", metavar="FILE", help="从文件读取 URL 列表 (一行一个)")
    parser.add_argument("--urls", nargs="+", metavar="URL", help="批量传入多个 URL")
    parser.add_argument("--compare", action="store_true", help="批量模式下生成综合对比分析")
    parser.add_argument("--text-only", action="store_true", help="仅提取字幕文本")
    parser.add_argument("--lang", default="zh", help="字幕语言偏好 (默认 zh)")
    args = parser.parse_args()

    if not args.url and not args.batch and not args.urls:
        parser.print_help()
        return 0

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
