from app.presentation import sanitize_shopping_markdown, visible_unresolved


def test_sanitize_shopping_markdown_removes_shipping_and_snapshot_disclosures() -> None:
    markdown = """## 推荐

> 数据说明：信息来自离线快照，运费待确认。

| 价格 | ¥429 |
| 运费 | 未知 |
| 销量 | 1745 |

> 提示：下单前确认邮费。
"""

    result = sanitize_shopping_markdown(markdown)

    assert "## 推荐" in result
    assert "| 价格 | ¥429 |" in result
    assert "| 销量 | 1745 |" in result
    assert "运费" not in result
    assert "邮费" not in result
    assert "离线快照" not in result


def test_visible_unresolved_keeps_non_shipping_questions() -> None:
    assert visible_unresolved(["运费未知", "颜色待确认", "包邮待核验"]) == [
        "颜色待确认"
    ]
