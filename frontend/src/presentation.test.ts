import { describe, expect, it } from "vitest";
import { sanitizeShoppingMarkdown, visibleResultStrings } from "./presentation";

describe("结果展示字段过滤", () => {
  it("从历史 Markdown 中删除运费行和统一数据说明", () => {
    const markdown = [
      "## 耳机推荐",
      "",
      "> **数据说明**：以下信息来自离线快照，价格与运费下单前复核。",
      "",
      "| **价格** | **¥429** |",
      "| **运费** | 待确认 |",
      "| **销量** | 1,745件 |",
      "",
      "> **提示**：请向店铺确认运费。",
    ].join("\n");

    const sanitized = sanitizeShoppingMarkdown(markdown);

    expect(sanitized).toContain("## 耳机推荐");
    expect(sanitized).toContain("| **价格** | **¥429** |");
    expect(sanitized).toContain("| **销量** | 1,745件 |");
    expect(sanitized).not.toMatch(/运费|离线快照|数据说明/);
  });

  it("保留其他未确认事项，仅移除运费相关项", () => {
    expect(visibleResultStrings(["运费未知", "颜色待确认", "包邮待核验"])).toEqual(["颜色待确认"]);
  });
});
