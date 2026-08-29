# Bilibili Summarizer 🎵

> B站视频 → AI 结构化总结，一条命令搞定  
> 下载字幕 · LLM 总结 · 批量处理 · 对比分析 · **LLM Wiki 体系** · **图可视化** · **3 状态机收藏夹自动转录**

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

# 6. 收藏夹批量转录 (3 状态机)
python3 transcribe_skill.py --auto
```

## LLM Wiki 体系

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

### 文件说明 (按目录组织)

```
bilibili-summarizer/
├── bilibili_cc.py            # 字幕下载 + SRT 转换 (根, 被 subprocess 调)
├── qr_login.py               # 二维码登录辅助
├── summarize.py              # 单视频/批量/对比 + ASR fallback (根, 被依赖)
├── test_bilibili_asr.py      # asr 模块测试
├── test_transcribe_skill.py  # transcribe 工作流测试 (98 个)
│
├── cli/                      # 🆕 CLI 入口
│   └── transcribe_cli.py     #    收藏夹批量转录主逻辑 (3 状态机 + LOCK/UNLOCK)
│
├── core/                     # 🆕 核心能力
│   ├── bilibili_api.py       #    (git mv from fetch.py)
│   ├── asr.py                #    (mv from bilibili_asr.py)
│   └── curator.py            #    (mv from bilibili_curator.py)
│
├── wiki/                     # 🆕 Wiki 系统
│   ├── gen.py                #    (git mv from wiki_gen.py)
│   ├── graph.py              #    (git mv from wiki_graph.py)
│   ├── chat.py               #    (git mv from wiki_chat.py)
│   ├── index.md
│   └── videos/
│
├── analyze/                  # 🆕 分析/聚类工具
│   ├── comments.py           #    (mv from bilibili_comments.py)
│   ├── danmaku.py            #    (mv from bilibili_danmaku.py)
│   ├── theme.py              #    (mv from theme_graph.py)
│   ├── theme_v2.py           #    (mv from theme_graph_v2.py)
│   ├── up_classifier.py      #    (mv from up_classifier.py)
│   ├── up_classify_active.py #    (mv from up_classify_active.py)
│   ├── up_classify_full.py   #    (mv from up_classify_full.py)
│   └── up_classify_quick.py  #    (mv from up_classify_quick.py)
│
├── (14 个根目录兼容 shim)     # 老 import 路径仍可用
│
└── explore/                  # 各种 API 探索脚本
```

### 根目录兼容 shim (老代码仍能用)

```python
# 老 import 路径仍可用 (向后兼容)
from fetch import *
from wiki_gen import *
import bilibili_asr  # 等同于 core.asr
import bilibili_curator  # 等同于 core.curator
import transcribe_skill  # 等同于 cli.transcribe_cli (共享 module 对象)
```

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

---

## 收藏夹自动转录 (`transcribe_skill.py` / `cli/transcribe_cli.py`) — 3 状态机

**场景**: 你在 B 站有「待总结」收藏夹堆了几十个待消化的视频, 想全自动转录 + 总结 + 归档, **不丢视频** (即使中间失败)。

### 3 状态机 (防丢设计)

```
未总结 (4115533556) → 总结中 (4012580756) → 已总结 (4090394056)
       ↑                  ↓
       └── 转录前失败     转录/移动失败 (留在「总结中」等下次 cron 重试)
```

| 阶段 | 操作 | 失败后视频在 |
|---|---|---|
| 🔒 LOCK | 未总结 → 总结中 | 未总结 (下次 cron 再试) |
| ⚙️ TRANSCRIBE | B站字幕 / ASR / LLM / wiki | 总结中 (不丢) |
| 🔓 UNLOCK | 总结中 → 已总结 | 总结中 (--move-done 重试) |

**任何失败都不丢视频**, 收藏夹就是 durable storage。

### 用法

```bash
# 全自动 (转录模式)
python3 transcribe_skill.py

# cron 用: 全自动 + 自动确认 + 写报告
python3 transcribe_skill.py --auto --move-done --report-to ~/my_bili_data/nightly_transcribe_report.md

# 只扫描 (看哪些视频)
python3 transcribe_skill.py --dry-run

# 长视频限制 (19 小时那个用这个)
python3 transcribe_skill.py --asr-max-duration 1800

# 测试单个视频 (不打乱收藏夹)
python3 transcribe_skill.py --bvid BV1xxx --skip-move

# --move-done 模式: 只检查「总结中」的视频, 真 summary 才移到「已总结」
python3 transcribe_skill.py --move-done --auto
```

### 总结质量校验

`--move-done` 模式: 视频要被移到「已总结」必须**所有分P summary.md 都合格**:

- ✅ size ≥ 1.5KB
- ✅ 5 段标记 ≥ 3 个 (`📺🧠💡🔑📐`)
- ✅ 不含 placeholder 字 (`[待] [失败] [placeholder] [占位] [stub]`)

### 输出结构

```
transcribed/
├── BV1xxx/                    # 单 P 视频
│   ├── meta.json
│   ├── transcript.srt
│   ├── transcript.txt
│   └── summary.md
└── BV2yyy/                    # 多分P 视频
    ├── meta.json
    ├── index.md               # 串起 P1..PN
    ├── P1/
    │   ├── transcript.srt
    │   ├── transcript.txt
    │   └── summary.md
    ├── P2/...
    └── P9/...
```

### OpenClaw cron (凌晨 3:30 自动跑)

```bash
openclaw cron add --name "bilibili-nightly-transcribe" \
  --schedule "30 3 * * *" \
  --command "python3 ~/my_proj/bilibili-summarizer/transcribe_skill.py --auto --move-done --report-to ~/my_bili_data/nightly_transcribe_report.md" \
  --timezone "Asia/Shanghai"
```

**报告路径**: `~/my_bili_data/nightly_transcribe_report.md` (HEARTBEAT 早 7-9 推到飞书)

### 测试

```bash
# 98 个测试 (93 pass / 1 fail = 「待总结」收藏夹当前真为空, 非代码 bug)
python3 test_transcribe_skill.py
```

---

## License

MIT
