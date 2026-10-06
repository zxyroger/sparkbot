"""``python -m sparkbot.mock_device`` 入口。"""

from __future__ import annotations

import sys

# 依赖路径引导必须先于任何第三方导入。
from ..paths import ensure_path, install_hint, missing_dependencies

ensure_path()

from .device import main  # noqa: E402

if __name__ == "__main__":
    missing = missing_dependencies()
    if missing:
        print(f"缺少依赖: {', '.join(missing)}\n\n{install_hint()}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
