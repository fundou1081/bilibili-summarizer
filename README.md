# Bilibili Summarizer 🎵

> B站视频 → AI 结构化总结，一条命令搞定  
> 下载字幕 · LLM 总结 · 批量处理 · 对比分析 · **LLM Wiki 体系** · **图可视化**

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
```

## LLM Wiki 体系 (新增)

```bash
# A. 批量拉取 (收藏夹/稍后观看/followings/UP主)
python3 fetch.py favorites --incremental
python3 fetch.py followings --incremental
python3 fetch.py all --incremental  # 适合 cron

# B. 生成 LLM Wiki (.md)
python3 wiki_gen.py --downloads ./downloads/ --output ./wiki/

# C. 图可视化
python3 wiki_graph.py --input ./wiki/ --output graph.png
python3 wiki_graph.py --input ./wiki/ --output graph.html --format html

# D. 多轮对话 (CLI, Skill 渐进性披露)
python3 wiki_chat.py
```

### 完整工作流 (适合 cron)

```bash
# 1. 增量拉取 (跳过已下载的)
python3 fetch.py all --incremental

# 2. 重新生成 .md
python3 wiki_gen.py

# 3. 重新画图
python3 wiki_graph.py -o graph.png

# crontab (每天 4 点跑):
0 4 * * * cd /path/to/bilibili-summarizer && python3 fetch.py all --incremental && python3 wiki_gen.py && python3 wiki_graph.py -o graph.png
```

### 文件说明

| 文件 | 用途 |
|------|------|
| `bilibili_cc.py` | 字幕下载 + SRT 转换工具 |
| `summarize.py` | AI 总结工具 (单视频/批量/对比) |
| `qr_login.py` | 二维码登录辅助 |
| **`fetch.py`** | **批量拉取 B 站数据** (收藏夹/稍后观看/followings/UP主) |
| **`wiki_gen.py`** | **从 downloads/ 生成 LLM Wiki .md** |
| **`wiki_graph.py`** | **画视频-标签-UP主关联图 (PNG/HTML)** |
| **`wiki_chat.py`** | **Wiki 多轮对话 (CLI, Skill 渐进性披露)** |
| `explore/` | 各种 API 探索脚本 (fetch.py 复用) |

### LLM Wiki 输出结构

```
wiki/
├── index.md                    # 所有视频的索引
└── videos/
    ├── BV1xxx_标题.md         # 每个视频一个 .md
    ├── BV2yyy_标题.md         # 含 frontmatter / 标签 / 分P / 字幕摘要
    └── ...
```

## 支持的 LLM

通过 OpenAI 兼容 API 调用，设置 `DEEPSEEK_API_KEY` 环境变量即可使用默认的 DeepSeek。

也支持：MiniMax, 智谱 GLM, Ollama 本地, 任意 OpenAI 兼容端点。

## License

MIT
