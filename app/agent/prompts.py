"""Load prompt configuration from ``app/prompt/prompts.yml``."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import get_settings


@lru_cache
def load_prompts(path: str | Path | None = None) -> dict[str, Any]:
    prompt_path = Path(path or get_settings().prompt_file)
    with prompt_path.open(encoding="utf-8") as source:
        content = yaml.safe_load(source) or {}
    if not isinstance(content, dict):
        raise ValueError(f"Prompt 文件顶层必须是对象: {prompt_path}")
    return content


def get_prompt(key: str, default: str = "") -> str:
    """Read a dot-separated prompt key, for example ``system.base``."""

    value: Any = load_prompts()
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value if isinstance(value, str) else default


MAIN_SYSTEM_PROMPT = """你是 globuy，一个帮助用户规划购物、搜索商品和比较价格的助手。
明确区分事实、推断和暂未接入的能力，不要编造商品、价格或库存。"""
