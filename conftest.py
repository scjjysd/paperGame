"""根级 conftest：确保 pytest 能从仓库根导入 app 包。

pyproject.toml 已配置 pythonpath = ["."]，此文件作为兼容兜底，
同时为未来共享 fixture 预留位置。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
