"""Prompt 模板加载器。

从 config/prompts/*.txt 加载文本模板，使用 {variable} 占位符（Python str.format 语法），
基于 mtime 缓存支持热更新，无需重启服务即可调整提示词。
"""

from __future__ import annotations

from pathlib import Path


class PromptLoader:
    """加载并渲染 prompt 模板文件。"""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self._cache: dict[str, tuple[int, str]] = {}

    def render(self, name: str, **kwargs) -> str:
        """加载模板文件 {name}.txt 并用 kwargs 填充占位符。

        Args:
            name: 模板名（不含 .txt 后缀），相对于 base_dir 的路径。
                  例如 "plan_generation" → {base_dir}/plan_generation.txt
                       "schema_comment/database_context" → {base_dir}/schema_comment/database_context.txt
            **kwargs: 模板中的 {key} 占位符将替换为对应的值。

        Returns:
            填充后的完整 prompt 文本。

        Raises:
            FileNotFoundError: 模板文件不存在且无内置兜底。
        """
        path = self.base_dir / f"{name}.txt"
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            raise FileNotFoundError(f"Prompt 模板不存在: {path}")

        cached = self._cache.get(name)
        if cached is None or cached[0] != mtime:
            template = path.read_text(encoding="utf-8")
            self._cache[name] = (mtime, template)
        else:
            template = cached[1]
        return template.format(**kwargs)

    def reload(self) -> None:
        """清空缓存，下次 render 强制重读文件。"""
        self._cache.clear()
