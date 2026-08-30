#!/bin/bash
# run_naily_transcribe.sh — 上午 9:20 自动转录 + 移动 wrapper
#
# 用法:
#   ./run_nightly_transcribe.sh    # 实际跑
#   ./run_nightly_transcribe.sh --dry-run  # 只扫描不转录 (测试)
#
# 注册到 OpenClaw cron (9:20 AM Asia/Shanghai):
#   openclaw cron add --name bilibili-daytime-transcribe \
#     --cron "20 9 * * *" --tz "Asia/Shanghai" \
#     --command "/Users/fundou/my_proj/bilibili-summarizer/run_nightly_transcribe.sh" \
#     --command-cwd "/Users/fundou/my_proj/bilibili-summarizer" \
#     --timeout-seconds 7200
#
# 工作流:
#   1. cd 到项目目录 (确保 .env 可读)
#   2. set -e (任何错误退出)
#   3. source .env 加载 MINIMAX_API_KEY (注入本地不打印)
#   4. 跑 transcribe_skill --auto --move-done --report-to ~/my_bili_data/nightly_transcribe_report.md
#   5. 退出码: 0=全成功, 非0=有失败
#   6. HEARTBEAT.md 9-10 AM 读 nightly_transcribe_report.md 推飞书
#
# 注意: 不在 chat 里打印 API key 内容 (飞书脱敏 + 安全)

set -e

# 切到项目根 (保证 .env 路径正确)
cd "$(dirname "$0")"

# 确保 log 目录
mkdir -p logs

# 关键: 找带 bilibili_api 的 Python (cron 默认 PATH 不含 miniconda)
# 优先 miniconda3, 其次用户 PATH 里的 python3
PYTHON=""
if [ -x "$HOME/miniconda3/bin/python3" ]; then
    PYTHON="$HOME/miniconda3/bin/python3"
elif [ -x "/opt/homebrew/bin/python3" ]; then
    PYTHON="/opt/homebrew/bin/python3"
else
    PYTHON=$(command -v python3 2>/dev/null || echo "/usr/bin/python3")
fi

# 验证选中的 Python 真的有 bilibili_api
if ! "$PYTHON" -c "import bilibili_api" 2>/dev/null; then
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] ✗ 选择的 Python 没有 bilibili_api: $PYTHON" | tee -a logs/cron_error.log
    exit 1
fi

# 时间戳
TS=$(date +"%Y-%m-%d %H:%M:%S")
LOG_FILE="logs/nightly_${TS// /_}.log"

echo "[$TS] 开始夜间转录 (auto + move-done) [Python: $PYTHON]" | tee -a "$LOG_FILE"

# 加载 .env (python summarize._load_dotenv() 会在脚本里自己 load, bash 不再 source
# 因为 .env 含中文/括号注释, bash source 会 parse 错误)
if [ -f .env ]; then
    KEY_LEN=$(grep '^MINIMAX_API_KEY=' .env | cut -d= -f2 | tr -d '"' | wc -c)
    echo "[$TS] ✓ .env 已存在 (MINIMAX_API_KEY 长度: $((KEY_LEN - 1)))" >> "$LOG_FILE"
else
    echo "[$TS] ⚠️  .env 不存在, MINIMAX_API_KEY 可能未设置" | tee -a "$LOG_FILE"
fi

# 跑主脚本
EXIT_CODE=0
"$PYTHON" transcribe_skill.py \
    --auto \
    --move-done \
    --report-to "$HOME/my_bili_data/nightly_transcribe_report.md" \
    >> "$LOG_FILE" 2>&1 \
    || EXIT_CODE=$?

END_TS=$(date +"%Y-%m-%d %H:%M:%S")
echo "[$END_TS] 退出码: $EXIT_CODE" | tee -a "$LOG_FILE"

# 不删除 .env (下次跑还要用)
# 但 .env 不在 git 里 (已 gitignore)

exit $EXIT_CODE
