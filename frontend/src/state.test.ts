import { describe, expect, it } from "vitest";
import { initialState, workbenchReducer } from "./state";
import type { MonitorEvent } from "./types";

function event(sequence: number, name: MonitorEvent["event"], data: Record<string, unknown>): MonitorEvent {
  return {
    type: "monitor_event",
    schema_version: "1.0",
    event: name,
    event_id: `run-1:${sequence}`,
    sequence,
    thread_id: "thread-1",
    run_id: "run-1",
    timestamp: "2026-07-20T00:00:00Z",
    data,
  };
}

describe("workbenchReducer", () => {
  it("按 message_id 拼接文本，并用 sequence 去重", () => {
    const started = workbenchReducer(initialState, {
      type: "EVENT",
      event: event(1, "TEXT_MESSAGE_START", { message_id: "message-1", role: "assistant" }),
    });
    const content = workbenchReducer(started, {
      type: "EVENT",
      event: event(2, "TEXT_MESSAGE_CONTENT", { message_id: "message-1", delta: "你好" }),
    });
    const duplicate = workbenchReducer(content, {
      type: "EVENT",
      event: event(2, "TEXT_MESSAGE_CONTENT", { message_id: "message-1", delta: "你好" }),
    });

    expect(duplicate.messages).toHaveLength(1);
    expect(duplicate.messages[0].content).toBe("你好");
    expect(duplicate.lastSequence).toBe(2);
  });

  it("连接控制事件不会进入业务轨迹", () => {
    const next = workbenchReducer(initialState, {
      type: "EVENT",
      event: { ...event(1, "CUSTOM", { name: "heartbeat" }), sequence: null },
    });
    expect(next.steps).toEqual([]);
    expect(next.toolCalls).toEqual([]);
    expect(next.forks).toEqual([]);
  });

  it("初始化事件只作为临时状态，不会写入对话消息", () => {
    const preparing = workbenchReducer(initialState, {
      type: "EVENT",
      event: { ...event(1, "CUSTOM", { name: "conversation_initializing", phase: "preparing" }), message: "收到你的消息了~正在初始化本次对话" },
    });
    const thinking = workbenchReducer(preparing, {
      type: "EVENT",
      event: event(2, "STEP_STARTED", { phase: "think", iteration: 0 }),
    });

    expect(preparing.initializationMessage).toBe("收到你的消息了~正在初始化本次对话");
    expect(preparing.messages).toEqual([]);
    expect(thinking.initializationMessage).toBeNull();
  });

  it("聚合同一个工具调用的参数、结果和终态", () => {
    const start = workbenchReducer(initialState, {
      type: "EVENT",
      event: event(1, "TOOL_CALL_START", { tool_call_id: "tool-1", tool_name: "web_search" }),
    });
    const args = workbenchReducer(start, {
      type: "EVENT",
      event: event(2, "TOOL_CALL_ARGS", { tool_call_id: "tool-1", arguments: { query: "耳机" } }),
    });
    const end = workbenchReducer(args, {
      type: "EVENT",
      event: event(3, "TOOL_CALL_END", { tool_call_id: "tool-1", status: "ok", duration_ms: 18 }),
    });
    expect(end.toolCalls).toHaveLength(1);
    expect(end.toolCalls[0]).toMatchObject({ name: "web_search", status: "succeeded", durationMs: 18 });
    expect(end.toolCalls[0].arguments).toEqual({ query: "耳机" });
  });
});
