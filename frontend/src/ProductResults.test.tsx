// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ProductResults } from "./ProductResults";
import type { TaskResult } from "./types";

const result: TaskResult = {
  status: "complete",
  final_text: "完成",
  picks: [
    { item_id: "one", title: "候选耳机 A", price: 299, currency: "CNY", platform: "jingdong", rating: 4.8, sales: 1280, image_url: "https://img.example.com/a.jpg", product_url: "https://example.com/a", reasons: ["预算内"], flags: ["运费待确认", "库存较少"] },
    { item_id: "two", title: "候选耳机 B", price: 399, currency: "CNY", platform: "taobao", rating: null, sales: null, product_url: "https://example.com/b", reasons: ["检索顺位 2"] },
  ],
  unresolved: ["运费未知", "颜色待确认"],
  learned_preferences: [],
  memory_status: "not_configured",
  source_kind: "offline_snapshot",
  artifacts: [],
};

afterEach(cleanup);

describe("商品结果", () => {
  it("未执行商品搜索时不展示 Shortlist", () => {
    render(<ProductResults artifacts={[]} result={{ ...result, picks: [] }} />);
    expect(screen.queryByText("SHORTLIST")).not.toBeInTheDocument();
    expect(screen.queryByText("暂未形成结构化商品清单")).not.toBeInTheDocument();
  });

  it("商品搜索完成且没有候选时才展示空 Shortlist", () => {
    render(<ProductResults artifacts={[]} result={{
      ...result,
      picks: [],
      search_attempted: true,
      search_candidate_count: 0,
    }} />);
    expect(screen.getByText("SHORTLIST")).toBeVisible();
    expect(screen.getByText("暂未形成结构化商品清单")).toBeVisible();
  });

  it("搜索曾返回候选但没有精选结果时不误报候选为空", () => {
    render(<ProductResults artifacts={[]} result={{
      ...result,
      picks: [],
      search_attempted: true,
      search_candidate_count: 2,
    }} />);
    expect(screen.queryByText("SHORTLIST")).not.toBeInTheDocument();
  });

  it("优先渲染接口返回的真实商品图片 URL", () => {
    render(<ProductResults artifacts={[]} result={result} />);
    expect(screen.getByRole("img", { name: "候选耳机 A" })).toHaveAttribute("src", "https://img.example.com/a.jpg");
  });

  it("仅展示有值的评分和销量，且不展示运费", () => {
    render(<ProductResults artifacts={[]} result={result} />);
    expect(screen.getByText("评分 4.8")).toBeVisible();
    expect(screen.getByText("销量 1,280")).toBeVisible();
    expect(screen.queryByText("未提供")).not.toBeInTheDocument();
    expect(screen.queryByText(/运费/)).not.toBeInTheDocument();
    expect(screen.queryByText("离线数据快照")).not.toBeInTheDocument();
    expect(screen.queryByText(/价格与库存来自离线快照/)).not.toBeInTheDocument();
    expect(screen.getByText("库存较少")).toBeVisible();
    expect(screen.getByText("颜色待确认")).toBeVisible();
  });

  it("允许选择两件商品并打开真实字段对比", () => {
    render(<ProductResults artifacts={[]} result={result} />);
    const toggles = screen.getAllByRole("button", { name: "加入对比" });
    fireEvent.click(toggles[0]);
    fireEvent.click(toggles[1]);
    fireEvent.click(screen.getByRole("button", { name: "对比商品" }));
    expect(screen.getByRole("dialog", { name: "商品对比" })).toBeInTheDocument();
    expect(screen.getAllByText("候选耳机 A")).toHaveLength(2);
    expect(screen.getAllByText("¥ 299.00")).toHaveLength(2);
  });
});
