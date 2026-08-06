"""YAML 配置加载器:本地缓存 + 基于 mtime 的热更新。

所有规则文件都通过这里读取,不写死在代码里。文件改动后无需重启服务,
下一次读取时 mtime 变化即自动重载(毫秒级 mtime 比对,不追求实时监听)。
"""

from __future__ import annotations

from pathlib import Path

import yaml


class ConfigLoader:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self._cache: dict[str, tuple[int, dict]] = {}

    def load(self, rel_path: str) -> dict:
        """读取并缓存一个 yaml;文件被修改后自动重载。"""
        path = self.base_dir / rel_path
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            raise FileNotFoundError(f"配置文件不存在: {path}")
        cached = self._cache.get(str(rel_path))
        if cached is not None and cached[0] == mtime:
            return cached[1]
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        self._cache[str(rel_path)] = (mtime, data)
        return data

    def reload(self) -> None:
        self._cache.clear()
