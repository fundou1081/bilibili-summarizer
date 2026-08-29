"""兼容 shim: transcribe_skill → cli.transcribe_cli

让 `import transcribe_skill as ts` 和 `import cli.transcribe_cli as cli`
共用同一个模块对象, 这样 `patch.object(ts, 'scan_favorites')` 和
`patch.object(cli, 'scan_favorites')` patch 的是同一个函数。

调用方式不变:
  python3 transcribe_skill.py --auto
  python3 transcribe_skill.py --bvid BV1xxx --yes
  python3 transcribe_skill.py --move-done --auto
"""
import sys
import os
from pathlib import Path

# 找 shim 自己的位置
if __file__ and __file__ != "<stdin>":
    _THIS_DIR = Path(__file__).resolve().parent
else:
    # stdin / 交互模式 — 用当前工作目录
    _THIS_DIR = Path(os.getcwd())

# 强制让 transcribe_skill 和 cli.transcribe_cli 是同一个模块对象
# 实现: 加载 cli.transcribe_cli 后, 把 sys.modules['transcribe_skill'] 设为同一个 module
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "_cli_transcribe_cli_alias",
    str(_THIS_DIR / "cli" / "transcribe_cli.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# 把 transcribe_skill 设为 cli.transcribe_cli 同一个 module
sys.modules[__name__] = _mod
# 同时保留 cli.transcribe_cli 引用
sys.modules.setdefault("cli.transcribe_cli", _mod)

if __name__ == "__main__":
    _mod.main()