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
python3 transcribe_skill.py --auto --move-done --report-to ~/my_bili_data/nightly_transcribe_report.md  # B1: 默认扫 2 源 (总结中 优先, 待总结 orphan 兜底)

# 只扫描 (看哪些视频)
python3 transcribe_skill.py --dry-run

# 长视频限制 (19 小时那个用这个)
python3 transcribe_skill.py --asr-max-duration 1800

# 测试单个视频 (不打乱收藏夹)
python3 transcribe_skill.py --bvid BV1xxx --skip-move

# --move-done 模式 (B1, 2026-08-31): 默认扫 2 个源 (总结中优先, 一个一个看), 真 summary 才移到「已总结」
#   总结中 (4012580756, LOCK 状态, 优先) → 待总结 (4115533556, orphan 兜底, e.g. 人工转录没走过 LOCK)
python3 transcribe_skill.py --move-done --auto
```

### 总结质量校验

`--move-done` 模式 (B1 改进): 视频要被移到「已总结」必须**所有分P summary.md 都合格**, 默认扫 2 源 (总结中 优先, 待总结 兜底):

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

---

## ⚠️ 开发事故记录 (2026-08-29)

### transcribe_skill.py 重构覆盖事故

**时间**: 2026-08-29 13:31 → 14:46 (Plan B 重构期间)

**根因 (按时序)**:
1. `git mv transcribe_skill.py cli/` 失败 (fatal: not under version control)
2. **没意识到**这意味着文件没 commit 过 → `cat > transcribe_skill.py` 写 shim 时**直接覆盖了原文件**
3. 之后 `cp transcribe_skill.py cli/transcribe_cli.py && rm transcribe_skill.py` 复制的是 shim, 不是原码
4. `__pycache__/transcribe_skill.cpython-311.pyc` 是 shim 编译产物 (813 字节), 没用

**灾难性后果**:
- `transcribe_skill.py` 是 3 状态机核心 (LOCK/UNLOCK + --move-done + _check_all_summaries)
- **从未 commit 过** (git 历史完全无)
- 无 .bak / 无 stash / 无 reflog 备份

**恢复**:
- 从对话历史 edit 记录重建 (所有上文的 edit/测试输出保留)
- 重建为 `cli/transcribe_cli.py` (23492 字节, 655 行, 含 LOCK/UNLOCK 3 状态机)
- 根目录 `transcribe_skill.py` 改 shim (1166 字节, 用 `sys.modules[__name__] = _mod` 让 ts 和 cli 共享同一 module 对象)

**Shim 关键设计** (patch 生效):
```python
# 强制让 transcribe_skill 和 cli.transcribe_cli 是同一个 module 对象
# 这样 patch.object(ts, 'scan_favorites') 和 patch.object(cli, 'scan_favorites') patch 同一个函数
sys.modules[__name__] = _mod
sys.modules.setdefault("cli.transcribe_cli", _mod)
```

**教训 (4 条)**:
1. **移动 untracked 文件前**: 必须先 `git add` 或确认有备份
2. **`cat > file.py` 是覆盖语义, 不是 append**: 写 shim 时必须确认原文件无重要内容
3. **git mv 失败 = "fatal: not under version control"**: 说明文件没 commit, 不是 "文件不存在"
4. **未 commit 的代码 = 易失**: 必须先 commit 再 refactor

**测试状态** (恢复后): 93 pass / 1 fail
- 1 fail = `scan_favorites` 真调 B 站 API, 返回 0 视频 (「待总结」收藏夹当前真为空, 非代码 bug)

**恢复 commits**:
- `5b0fac9 refactor: 重构代码到 cli/core/wiki/analyze 子目录`
- `7992146 feat: 收藏夹批量转录 3 状态机 (cli/transcribe_cli + 兼容 shim)`
