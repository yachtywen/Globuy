"""Category-level buying dimensions and risk hints."""

from langchain_core.tools import tool

_CATEGORY_HINTS = {
    "耳机": ["佩戴舒适度", "降噪", "续航", "编解码", "麦克风"],
    "键盘": ["配列", "轴体", "连接方式", "键帽", "延迟"],
    "手机": ["系统", "屏幕", "影像", "续航", "频段与保修"],
    "电脑": ["处理器", "显卡", "内存", "屏幕", "散热与重量"],
}


@tool
def category_insight(category: str) -> dict:
    """Return important comparison dimensions for a product category."""

    key = next((name for name in _CATEGORY_HINTS if name in category), None)
    dimensions = _CATEGORY_HINTS.get(
        key,
        ["核心功能", "兼容性", "可靠性", "售后", "总到手成本"],
    )
    return {
        "status": "ok",
        "category": category.strip(),
        "dimensions": dimensions,
        "risks": ["地区版本差异", "保修限制", "价格与库存时效性"],
        "source": "built-in-framework-knowledge",
    }
