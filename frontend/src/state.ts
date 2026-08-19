import type {
  Artifact,
  CatalogProgress,
  ChatMessage,
  ConnectionStatus,
  ForkTrace,
  MonitorEvent,
  RunStatus,
  RuntimeStep,
  TaskResult,
  ThreadDetail,
  ThreadSummary,
  ToolCall,
  ViewMode,
} from "./types";

export interface WorkbenchState {
  threads: ThreadSummary[];
  archiveCursor: string | null;
  activeThreadId: string | null;
  viewingThreadId: string | null;
  viewMode: ViewMode;
  detail: ThreadDetail | null;
  threadLoading: boolean;
  archiveCreating: boolean;
  connectionStatus: ConnectionStatus;
  taskStatus: RunStatus | "idle";
  currentRunId: string | null;
  lastSequence: number;
  messages: ChatMessage[];
  toolCalls: ToolCall[];
  steps: RuntimeStep[];
  forks: ForkTrace[];
  initializationMessage: string | null;
  catalogProgress: CatalogProgress | null;
  result: TaskResult | null;
  artifacts: Artifact[];
  error: string | null;
}

export const initialState: WorkbenchState = {
  threads: [],
  archiveCursor: null,
  activeThreadId: null,
  viewingThreadId: null,
  viewMode: "active",
  detail: null,
  threadLoading: true,
  archiveCreating: false,
  connectionStatus: "idle",
  taskStatus: "idle",
  currentRunId: null,
  lastSequence: 0,
  messages: [],
  toolCalls: [],
  steps: [],
  forks: [],
  initializationMessage: null,
  catalogProgress: null,
  result: null,
  artifacts: [],
  error: null,
};

export type WorkbenchAction =
  | { type: "LOAD_START" }
  | { type: "SET_THREADS"; active: ThreadSummary | null; archived: ThreadSummary[]; cursor: string | null }
  | { type: "APPEND_THREADS"; archived: ThreadSummary[]; cursor: string | null }
  | { type: "OPEN_DETAIL"; detail: ThreadDetail; mode: ViewMode }
  | { type: "MERGE_DETAIL"; detail: ThreadDetail }
  | { type: "ARCHIVE_START" }
  | { type: "ARCHIVE_END" }
  | { type: "TASK_ACCEPTED"; runId: string; query: string; createdAt: string }
  | { type: "TASK_STATUS"; status: RunStatus | "idle" }
  | { type: "CONNECTION"; status: ConnectionStatus }
  | { type: "EVENT"; event: MonitorEvent }
  | { type: "RUN_SYNC"; status: RunStatus; result: TaskResult | null; artifacts: Artifact[]; error?: string | null }
  | { type: "ERROR"; message: string | null };

function currentResult(detail: ThreadDetail): { status: RunStatus | "idle"; runId: string | null; result: TaskResult | null; artifacts: Artifact[] } {
  const run = detail.runs.at(-1);
  return {
    status: run?.status ?? "idle",
    runId: run?.run_id ?? null,
    result: run?.result ?? null,
    artifacts: run?.artifacts ?? [],
  };
}

function upsertMessage(messages: ChatMessage[], messageId: string, mutate: (message: ChatMessage) => ChatMessage): ChatMessage[] {
  const index = messages.findIndex((message) => message.message_id === messageId);
  if (index < 0) {
    return [
      ...messages,
      mutate({
        message_id: messageId,
        run_id: null,
        role: "assistant",
        content: "",
        is_partial: false,
        created_at: new Date().toISOString(),
        streaming: true,
      }),
    ];
  }
  return messages.map((message, itemIndex) => (itemIndex === index ? mutate(message) : message));
}

function eventState(state: WorkbenchState, event: MonitorEvent): WorkbenchState {
  if (event.sequence !== null && event.sequence <= state.lastSequence) return state;
  const lastSequence = event.sequence ?? state.lastSequence;
  const data = event.data;
  if (event.event === "RUN_STARTED") return { ...state, lastSequence, taskStatus: "running", catalogProgress: null };
  if (event.event === "TEXT_MESSAGE_START") {
    const id = String(data.message_id || event.event_id);
    return {
      ...state,
      lastSequence,
      messages: upsertMessage(state.messages, id, (message) => ({ ...message, run_id: event.run_id, streaming: true })),
    };
  }
  if (event.event === "TEXT_MESSAGE_CONTENT") {
    const id = String(data.message_id || event.event_id);
    const delta = typeof data.delta === "string" ? data.delta : "";
    return {
      ...state,
      lastSequence,
      messages: upsertMessage(state.messages, id, (message) => ({
        ...message,
        run_id: event.run_id,
        content: `${message.content}${delta}`,
        streaming: true,
      })),
    };
  }
  if (event.event === "TEXT_MESSAGE_END") {
    const id = String(data.message_id || event.event_id);
    return {
      ...state,
      lastSequence,
      messages: upsertMessage(state.messages, id, (message) => ({
        ...message,
        is_partial: Boolean(data.partial),
        streaming: false,
      })),
    };
  }
  if (event.event === "STEP_STARTED" || event.event === "STEP_FINISHED") {
    const phase = String(data.phase || "unknown");
    const iteration = Number(data.iteration || 0);
    const id = `${phase}:${iteration}`;
    const step: RuntimeStep = {
      id,
      phase,
      iteration,
      status: event.event === "STEP_STARTED" ? "running" : "succeeded",
      timestamp: event.timestamp,
    };
    return {
      ...state,
      lastSequence,
      initializationMessage: event.event === "STEP_STARTED" ? null : state.initializationMessage,
      steps: [...state.steps.filter((item) => item.id !== id), step],
    };
  }
  if (event.event.startsWith("TOOL_CALL_")) {
    const id = String(data.tool_call_id || event.event_id);
    const prior = state.toolCalls.find((tool) => tool.id === id);
    const tool: ToolCall = {
      id,
      name: String(data.tool_name || prior?.name || "工具调用"),
      status:
        event.event === "TOOL_CALL_END"
          ? data.status === "error" || data.status === "phase_rejected"
            ? "failed"
            : "succeeded"
          : "running",
      arguments: (data.arguments as Record<string, unknown>) ?? prior?.arguments,
      result: data.result ?? prior?.result,
      durationMs: typeof data.duration_ms === "number" ? data.duration_ms : prior?.durationMs,
      startedAt: prior?.startedAt ?? event.timestamp,
    };
    return { ...state, lastSequence, toolCalls: [...state.toolCalls.filter((item) => item.id !== id), tool] };
  }
  if (event.event === "CUSTOM" && data.name === "agent_fork") {
    const fork: ForkTrace = {
      id: event.event_id,
      childThreadId: String(data.child_thread_id || ""),
      reason: String(data.reason || "并行分析"),
      toolNames: Array.isArray(data.tool_names) ? data.tool_names.map(String) : [],
      timestamp: event.timestamp,
    };
    return { ...state, lastSequence, forks: [...state.forks, fork] };
  }
  if (event.event === "CUSTOM" && data.name === "conversation_initializing") {
    const message = event.message || String(data.message || "收到你的消息了~正在初始化本次对话");
    return { ...state, lastSequence, initializationMessage: message };
  }
  if (event.event === "CUSTOM" && data.name === "replay_gap") {
    return { ...state, lastSequence, catalogProgress: null };
  }
  if (event.event === "CUSTOM" && typeof data.name === "string" && [
    "shopping_intent_resolved", "catalog_cache_checked", "catalog_fetch_started",
    "catalog_fetch_progress", "catalog_fetch_finished", "catalog_normalization_progress",
    "catalog_persistence_progress", "catalog_index_progress", "hybrid_retrieval_progress",
  ].includes(data.name)) {
    const stageMap: Record<string, CatalogProgress["stage"]> = {
      shopping_intent_resolved: "intent", catalog_cache_checked: "cache",
      catalog_fetch_started: "fetch", catalog_fetch_progress: "fetch", catalog_fetch_finished: "fetch",
      catalog_normalization_progress: "normalize", catalog_persistence_progress: "persist",
      catalog_index_progress: "index", hybrid_retrieval_progress: "retrieve",
    };
    const prior = state.catalogProgress;
    const platform = typeof data.platform === "string" ? data.platform : null;
    const platforms = { ...(prior?.platforms ?? {}) };
    if (platform) platforms[platform] = {
      status: String(data.status ?? platforms[platform]?.status ?? "running"),
      accepted: Number(data.platform_total ?? data.accepted ?? platforms[platform]?.accepted ?? 0),
    };
    return {
      ...state, lastSequence, initializationMessage: null,
      catalogProgress: {
        stage: stageMap[data.name],
        message: event.message || String(data.message || prior?.message || "正在更新商品目录"),
        total: Number(data.deduplicated_total ?? data.total ?? data.fresh_candidates ?? prior?.total ?? 0),
        target: Number(data.target ?? prior?.target ?? 100),
        status: String(data.status ?? prior?.status ?? "running"),
        partialPlatforms: Array.isArray(data.partial_platforms) ? data.partial_platforms.map(String) : prior?.partialPlatforms ?? [],
        platforms,
      },
    };
  }
  if (event.event === "CUSTOM" && data.name === "task_result") {
    return { ...state, lastSequence, result: (data.result as TaskResult) ?? null };
  }
  if (event.event === "RUN_FINISHED") return { ...state, lastSequence, initializationMessage: null, catalogProgress: null, taskStatus: "succeeded", connectionStatus: "idle" };
  if (event.event === "TASK_CANCELLED") return { ...state, lastSequence, initializationMessage: null, catalogProgress: null, taskStatus: "cancelled", connectionStatus: "idle" };
  if (event.event === "RUN_ERROR") {
    return {
      ...state,
      lastSequence,
      initializationMessage: null,
      catalogProgress: null,
      taskStatus: "failed",
      connectionStatus: "idle",
      error: event.message || String(data.message || "任务执行失败"),
    };
  }
  return { ...state, lastSequence };
}

export function workbenchReducer(state: WorkbenchState, action: WorkbenchAction): WorkbenchState {
  switch (action.type) {
    case "LOAD_START":
      return { ...state, threadLoading: true, error: null };
    case "SET_THREADS":
      return {
        ...state,
        threads: [...(action.active ? [action.active] : []), ...action.archived],
        archiveCursor: action.cursor,
        activeThreadId: action.active?.thread_id ?? null,
      };
    case "APPEND_THREADS": {
      const existing = new Set(state.threads.map((thread) => thread.thread_id));
      return {
        ...state,
        threads: [...state.threads, ...action.archived.filter((thread) => !existing.has(thread.thread_id))],
        archiveCursor: action.cursor,
      };
    }
    case "OPEN_DETAIL": {
      const latest = currentResult(action.detail);
      return {
        ...state,
        detail: action.detail,
        viewingThreadId: action.detail.thread_id,
        viewMode: action.mode,
        threadLoading: false,
        taskStatus: latest.status,
        currentRunId: latest.runId,
        messages: action.detail.messages,
        result: latest.result,
        artifacts: latest.artifacts,
        toolCalls: [],
        steps: [],
        forks: [],
        catalogProgress: null,
        lastSequence: 0,
        connectionStatus: "idle",
        error: null,
      };
    }
    case "MERGE_DETAIL": {
      const latest = currentResult(action.detail);
      return {
        ...state,
        detail: action.detail,
        messages: action.detail.messages,
        taskStatus: latest.status,
        currentRunId: latest.runId,
        result: latest.result,
        artifacts: latest.artifacts,
      };
    }
    case "ARCHIVE_START":
      return { ...state, archiveCreating: true, error: null };
    case "ARCHIVE_END":
      return { ...state, archiveCreating: false };
    case "TASK_ACCEPTED": {
      const optimistic: ChatMessage = {
        message_id: `optimistic_${action.runId}`,
        run_id: action.runId,
        role: "user",
        content: action.query,
        is_partial: false,
        created_at: action.createdAt,
      };
      return {
        ...state,
        currentRunId: action.runId,
        taskStatus: "starting",
        lastSequence: 0,
        messages: [...state.messages, optimistic],
        toolCalls: [],
        steps: [],
        forks: [],
        initializationMessage: null,
        catalogProgress: null,
        result: null,
        artifacts: [],
        error: null,
      };
    }
    case "TASK_STATUS":
      return { ...state, taskStatus: action.status };
    case "CONNECTION":
      return { ...state, connectionStatus: action.status };
    case "EVENT":
      return eventState(state, action.event);
    case "RUN_SYNC":
      return {
        ...state,
        taskStatus: action.status,
        result: action.result,
        artifacts: action.artifacts,
        catalogProgress: null,
        error: action.error ?? state.error,
      };
    case "ERROR":
      return { ...state, error: action.message, threadLoading: false, archiveCreating: false };
  }
}
