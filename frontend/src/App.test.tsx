// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { Markdown, NewThreadDialog, useConversationAutoScroll } from "./App";
import type { ChatMessage } from "./types";

function AutoScrollHarness({ messages }: { messages: ChatMessage[] }) {
  const end = useRef<HTMLDivElement>(null);
  useConversationAutoScroll(end, messages);
  return <div ref={end} />;
}

describe("安全 Markdown", () => {
  it("不执行原始 HTML，并为外链增加安全属性", () => {
    const { container } = render(
      <Markdown>{`<img src=x onerror="alert(1)">\n\n[来源](https://example.com)`}</Markdown>,
    );
    expect(container.querySelector("img")).not.toBeInTheDocument();
    const link = screen.getByRole("link", { name: "来源" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });
});

describe("新对话确认弹窗", () => {
  it("支持 Escape 关闭，并把焦点放在安全的取消操作上", () => {
    const cancel = vi.fn();
    render(
      <NewThreadDialog busy={false} onCancel={cancel} onConfirm={vi.fn()} open submitting={false} />,
    );
    expect(screen.getByRole("button", { name: "取消" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("运行中会显示先取消再归档的明确警告", () => {
    render(
      <NewThreadDialog busy onCancel={vi.fn()} onConfirm={vi.fn()} open submitting={false} />,
    );
    expect(screen.getByText(/先取消该任务/)).toBeInTheDocument();
  });
});

describe("对话自动滚动", () => {
  it("不会把 scrollIntoView 的返回值注册为 effect 清理函数", () => {
    const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, "scrollIntoView");
    const scrollIntoView = vi.fn(() => ({ unexpected: true }));
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });

    try {
      const first: ChatMessage[] = [];
      const second: ChatMessage[] = [{
        message_id: "message-1",
        run_id: "run-1",
        role: "user",
        content: "测试提交",
        is_partial: false,
        created_at: "2026-07-21T00:00:00Z",
      }];

      const view = render(<AutoScrollHarness messages={first} />);
      expect(() => view.rerender(<AutoScrollHarness messages={second} />)).not.toThrow();
      expect(() => view.unmount()).not.toThrow();
      expect(scrollIntoView).toHaveBeenCalledTimes(2);
    } finally {
      if (descriptor) Object.defineProperty(Element.prototype, "scrollIntoView", descriptor);
      else Reflect.deleteProperty(Element.prototype, "scrollIntoView");
    }
  });
});
