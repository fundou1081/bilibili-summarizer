# Bilibili Summarizer 🎵

> B站视频 → AI 结构化总结，一条命令搞定  
> 下载字幕 · LLM 总结 · 批量处理 · 对比分析

[![Python](https://img.shields.io/badge/python-3.9+-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## 安装

```bash
git clone https://github.com/fundou1081/bilibili-summarizer.git
cd bilibili-summarizer
pip install -r requirements.txt
```

## 快速使用

```bash
# 1. 扫码登录 (首次)
python3 bilibili_cc.py --login

# 2. 下载字幕 + 转 SRT
python3 bilibili_cc.py -c -d https://www.bilibili.com/video/BVxxxxxx

# 3. 一键 AI 总结 (需配 API Key)
export DEEPSEEK_API_KEY=sk-xxx
python3 summarize.py https://www.bilibili.com/video/BVxxxxxx

# 4. 批量总结
python3 summarize.py --batch urls.txt

# 5. 批量 + 对比分析
python3 summarize.py --batch urls.txt --compare

# 6. 仅提取文本
python3 summarize.py <URL> --text-only
```

## 文件说明

| 文件 | 用途 |
|------|------|
| `bilibili_cc.py` | 字幕下载 + SRT 转换工具 |
| `summarize.py` | AI 总结工具 (单视频/批量/对比) |
| `qr_login.py` | 二维码登录辅助 |

## 支持的 LLM

通过 OpenAI 兼容 API 调用，设置 `DEEPSEEK_API_KEY` 环境变量即可使用默认的 DeepSeek。

也支持：
- MiniMax (设置 `MINIMAX_API_KEY`)
- 智谱 GLM
- Ollama 本地
- 任意 OpenAI 兼容端点

修改 `summarize.py` 中的 `call_llm()` 函数即可切换。

## 支持的链接格式

- `https://www.bilibili.com/video/BVxxxxxx`
- `https://b23.tv/xxxxxx` (短链接)
- `BVxxxxxx` (纯 BV 号)
- 分享整段文本: `【标题】 https://b23.tv/xxx`

## 输出

```
downloads/<BVID>/
├── P1-中文.json          # 原始字幕
├── P1-中文.srt           # SRT 格式
└── transcript_summary.md # AI 总结
```

批量模式还会生成 `batch_index.json` 和 `compare_summary.md`。

## License

MIT
