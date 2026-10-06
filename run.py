"""项目根目录的启动器，用于不安装包直接运行。

用法::

    python run.py              # 启动 PC 端服务
    python run.py --provider deepseek --api-key sk-xxx

等价于 ``python -m sparkbot``，只是额外把项目根加入 ``sys.path``，
因此无需设置 ``PYTHONPATH`` 或执行 ``pip install -e .``。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sparkbot.paths import ensure_path  # noqa: E402

ensure_path()

from sparkbot.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
