// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { memoryApi, memorySkillApi } from "./api";
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

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("商品结果", () => {
  it("优先渲染接口返回的真实商品图片 URL", () => {
    render(<ProductResults artifacts={[]} result={result} />);
    expect(screen.getByRole("img", { name: "候选耳机 A" })).toHaveAttribute(
      "src",
      "/api/v1/product-image?image_url=https%3A%2F%2Fimg.example.com%2Fa.jpg",
    );
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

  it("实时报价未持久化时禁用加入心愿库", () => {
    render(<ProductResults artifacts={[]} result={{ ...result, picks: [{ ...result.picks[0], offer_id: "offer-live", wishlist_eligible: false }] }} />);
    expect(screen.getByRole("button", { name: "加入心愿库" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "加入心愿库" })).toHaveAttribute("title", "商品报价尚未保存，暂时无法加入心愿库。");
  });

  it("成功结果会自动打开候选偏好弹窗，取消时不会写入", async () => {
    vi.spyOn(memorySkillApi, "list").mockResolvedValue({
      items: [
        { skill_id: "general", name: "通用偏好", description: "", trigger_keywords: [], is_enabled: true, status: "active", memory_count: 0, created_at: "2026-07-25T00:00:00Z", updated_at: "2026-07-25T00:00:00Z" },
        { skill_id: "digital", name: "数码设备", description: "", trigger_keywords: ["耳机"], is_enabled: true, status: "active", memory_count: 0, created_at: "2026-07-25T00:00:00Z", updated_at: "2026-07-25T00:00:00Z" },
      ],
    });
    const confirm = vi.spyOn(memoryApi, "confirm").mockResolvedValue({ items: [], conflicts: [] });
    render(
      <ProductResults
        artifacts={[]}
        result={{
          ...result,
          learned_preferences: [
            {
              key: "budget_max_cny",
              category: "preference",
              content: "购物预算不超过 500 元",
              confidence: 1,
            },
          ],
        }}
        sourceRunId="run-1"
        sourceThreadId="thread-1"
      />,
    );

    expect(screen.getByText("发现 1 条可长期保留的偏好")).toBeVisible();
    expect(await screen.findByRole("dialog", { name: "确认长期记忆" })).toBeInTheDocument();
    expect(confirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog", { name: "确认长期记忆" })).not.toBeInTheDocument();
    expect(confirm).not.toHaveBeenCalled();
  });

  it("允许编辑候选、选择领域并只在确认后持久化", async () => {
    vi.spyOn(memorySkillApi, "list").mockResolvedValue({
      items: [
        { skill_id: "general", name: "通用偏好", description: "", trigger_keywords: [], is_enabled: true, status: "active", memory_count: 0, created_at: "2026-07-25T00:00:00Z", updated_at: "2026-07-25T00:00:00Z" },
        { skill_id: "digital", name: "数码设备", description: "", trigger_keywords: ["耳机"], is_enabled: true, status: "active", memory_count: 0, created_at: "2026-07-25T00:00:00Z", updated_at: "2026-07-25T00:00:00Z" },
      ],
    });
    const confirm = vi.spyOn(memoryApi, "confirm").mockResolvedValue({ items: [], conflicts: [] });
    render(<ProductResults artifacts={[]} result={{ ...result, learned_preferences: [{ key: "wearing", category: "preference", content: "喜欢头戴式耳机", confidence: 1 }] }} sourceRunId="run-2" sourceThreadId="thread-2" />);
    await screen.findByRole("dialog", { name: "确认长期记忆" });
    fireEvent.change(screen.getByRole("textbox", { name: "记忆内容" }), { target: { value: "优先头戴式降噪耳机" } });
    const selectors = screen.getAllByRole("combobox");
    fireEvent.change(selectors[1], { target: { value: "digital" } });
    expect(confirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));
    expect(confirm).toHaveBeenCalledWith(
      [expect.objectContaining({ content: "优先头戴式降噪耳机", skill_id: "digital" })],
      "thread-2",
      "run-2",
    );
  });

  it("成功的抖音候选会显示平台来源和商品卡片", () => {
    render(<ProductResults artifacts={[]} result={{ ...result, picks: [{ ...result.picks[0], item_id: "douyin:3", platform: "douyin", title: "抖音降噪耳机" }], platform_outcomes: [{ platform: "douyin", status: "ok", candidate_count: 1 }] }} />);
    expect(screen.getByRole("heading", { name: "抖音降噪耳机" })).toBeVisible();
    expect(screen.getByText("抖音")).toBeVisible();
    expect(screen.getByText("抖音 1 件候选")).toBeVisible();
  });

  it("图片加载失败时切换为本地占位图", () => {
    render(<ProductResults artifacts={[]} result={result} />);
    fireEvent.error(screen.getByRole("img", { name: "候选耳机 A" }));
    expect(screen.getAllByRole("img", { name: /商品图片暂缺/ })[0].getAttribute("src")).toMatch(/product-fallback/);
  });
});
