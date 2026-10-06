"""依赖路径引导。

本项目的第三方依赖安装在工作区内的 ``.vendor/`` 目录（而不是系统 site-packages），
这样可以零污染地部署、也方便整目录拷贝到另一台机器。

但这里有个陷阱：如果直接把 ``.vendor`` 加进 ``sys.path``，其中的
``vendor/`` 子目录名会与包名冲突，破坏 ``import sparkbot.*``。
因此本模块在**导入任何第三方库之前**先完成路径设置，
并且只把 ``.vendor`` 本身（而不是它的父目录）加入搜索路径。

典型用法（各入口文件的第一行）::

    from sparkbot.paths import ensure_path
    ensure_path()
"""

from __future__ import annotations

import sys
from pathlib import Path

#: 已经执行过引导，避免重复插入路径。
_BOOTSTRAPPED = False


def vendor_dir() -> Path:
    """返回工作区内的依赖目录 ``<项目根>/.vendor``。"""
    # 本文件位于 <项目根>/sparkbot/paths.py
    return Path(__file__).resolve().parent.parent / ".vendor"


def ensure_path(*extra: str | Path) -> list[str]:
    """把依赖目录加入 ``sys.path``，返回本次实际新增的路径。

    Args:
        extra: 额外的目录（例如开发时的源码根）。

    Returns:
        本次新增到 ``sys.path`` 的路径列表（已存在的不会重复添加）。
    """
    global _BOOTSTRAPPED

    candidates: list[Path] = []
    if not _BOOTSTRAPPED:
        candidates.append(vendor_dir())
    candidates.extend(Path(item) for item in extra)

    added: list[str] = []
    for path in candidates:
        if not path.is_dir():
            continue
        resolved = str(path)
        if resolved in sys.path:
            continue
        # 插到最前面，确保本地依赖优先于系统里的同名包。
        sys.path.insert(0, resolved)
        added.append(resolved)

    _BOOTSTRAPPED = True
    return added


def missing_dependencies() -> list[str]:
    """检查运行必需依赖，返回缺失的包名列表。

    只检查核心依赖；Pillow、openai 等可选依赖不在此列
    （它们的缺失只会让对应能力降级，不影响服务启动）。
    """
    required = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "websockets": "websockets",
        "httpx": "httpx",
        "pydantic": "pydantic",
        "pydantic_settings": "pydantic-settings",
    }
    missing: list[str] = []
    for module, package in required.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


def install_hint() -> str:
    """返回缺失依赖时的安装提示。"""
    return (
        "依赖未安装。请在项目根目录执行：\n"
        "    python -m pip install --target .vendor -r requirements.txt"
    )
