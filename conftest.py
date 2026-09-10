"""根级 conftest：确保 pytest 能从仓库根导入 app 包。

pyproject.toml 已配置 pythonpath = ["."]，此文件作为兼容兜底，
同时为未来共享 fixture 预留位置。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试期只写日志文件、不往 stderr 打：pytest 自己会捕获并在失败时展示日志，
# 再叠一份控制台输出只会让失败报告难以阅读。需要看控制台时用 LOG_CONSOLE=1 覆盖。
os.environ.setdefault('LOG_CONSOLE', '0')
