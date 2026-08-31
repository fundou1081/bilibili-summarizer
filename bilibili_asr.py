"""兼容 shim: bilibili_asr → core.asr (重构后兼容老 import)"""
from core.asr import *  # noqa: F401, F403
import sys

if __name__ == "__main__":
    # 之前漏了 __main__ 块, subprocess 调用时仅 import 就 exit 0, ASR 静默失败
    # 跟 transcribe_one 漏 await 是同类 bug: silent failure 把 ASR 隐藏
    sys.exit(main())
