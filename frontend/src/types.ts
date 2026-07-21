export type ThreadStatus = "active" | "archived";
export type RunStatus =
  | "starting"
  | "running"
  | "cancelling"
  | "succeeded"
  | "cancelled"
  | "failed"
  | "interrupted";

export type ConnectionStatus = "idle" | "connecting" | "connected" | "reconnecting" | "offline";
export type ViewMode = "active" | "archived";

export interface ThreadSummary {
  thread_id: string;
  title: string;
  status: ThreadStatus;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  archive_reason?: string | null;
  last_run_id?: string | null;
  last_run_status?: RunStatus | null;
  message_count?: number;
}

export interface Artifact {
  file_id: string;
  filename: string;
  kind: string;
  media_type: string;
  size: number;
  created_at: string;
  download_url: string;
}

export type ProductPick = Record<string, unknown> & {
  item_id: string;
  product_id?: string | null;
  offer_id?: string | null;
  platform: string;
  title: string;
  price: number | null;
  currency: string;
  rating: number | null;
  sales: number | null;
};

export interface WishlistItem {
  wishlist_item_id: string;
  offer_id: string;
  product_id: string;
  item_id: string;
  platform: string;
  title: string;
  image_url: string | null;
  product_url: string | null;
  added_price: number | null;
  current_price: number | null;
  currency: string;
  price_change: number | null;
  price_change_percent: number | null;
  rating: number | null;
  sales: number | null;
  status: "active" | "removed" | "purchased";
  target_price: number | null;
  note: string | null;
  added_at: string;
  last_checked_at: string | null;
  next_check_at: string | null;
  failure_count: number;
  last_error_code: string | null;
}

export interface Wishlist {
  wishlist_id: string;
  name: string;
  items: WishlistItem[];
}

export interface PriceObservation {
  observation_id: string;
  observed_at: string;
  price: number | null;
  currency: string;
}

export interface PriceHistory {
  wishlist_item_id: string;
  added_price: number | null;
  currency: string;
  items: PriceObservation[];
}

export interface MemoryEntry {
  memory_id: string;
  category: "blacklist" | "preference" | "history";
  key: string;
  content: string;
  confidence: number;
  source: "user" | "agent_confirmed" | "import";
  version: number;
  created_at: string;
  updated_at: string;
}

export interface TaskResult {
  status: "complete" | "incomplete" | "not_configured" | "error";
  final_text: string;
  picks: ProductPick[];
  unresolved: Array<unknown>;
  learned_preferences: Array<unknown>;
  memory_status: string;
  source_kind: "offline_snapshot" | string;
  artifacts: Artifact[];
}

export interface ChatMessage {
  message_id: string;
  run_id: string | null;
  role: "user" | "assistant" | "system";
  content: string;
  is_partial: boolean;
  ordinal?: number;
  created_at: string;
  streaming?: boolean;
}

export interface RunSummary {
  run_id: string;
  status: RunStatus;
  query: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  final_text?: string | null;
  result: TaskResult | null;
  artifacts: Artifact[];
  error_code?: string | null;
  error_message?: string | null;
}

export interface ThreadDetail extends ThreadSummary {
  read_only: boolean;
  messages: ChatMessage[];
  runs: RunSummary[];
}

export type MonitorEventName =
  | "RUN_STARTED"
  | "STEP_STARTED"
  | "STEP_FINISHED"
  | "TOOL_CALL_START"
  | "TOOL_CALL_ARGS"
  | "TOOL_CALL_RESULT"
  | "TOOL_CALL_END"
  | "TEXT_MESSAGE_START"
  | "TEXT_MESSAGE_CONTENT"
  | "TEXT_MESSAGE_END"
  | "RUN_FINISHED"
  | "RUN_ERROR"
  | "TASK_CANCELLED"
  | "CUSTOM";

export interface MonitorEvent {
  type: "monitor_event";
  schema_version: "1.0";
  event: MonitorEventName;
  event_id: string;
  sequence: number | null;
  thread_id: string;
  run_id: string;
  timestamp: string;
  message?: string | null;
  data: Record<string, unknown>;
}

export interface ToolCall {
  id: string;
  name: string;
  status: "running" | "succeeded" | "failed";
  arguments?: Record<string, unknown>;
  result?: unknown;
  durationMs?: number;
  startedAt: string;
}

export interface RuntimeStep {
  id: string;
  phase: string;
  iteration: number;
  status: "running" | "succeeded";
  timestamp: string;
}

export interface ForkTrace {
  id: string;
  childThreadId: string;
  reason: string;
  toolNames: string[];
  timestamp: string;
}

export interface RunStatusResponse {
  thread_id: string;
  run_id: string;
  status: RunStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  last_sequence: number;
  earliest_available_sequence: number | null;
  terminal_event: MonitorEvent | null;
  result: TaskResult | null;
  artifacts: Artifact[];
  memory_status: string;
  error: { code: string; message: string } | null;
}

export const TERMINAL_RUN_STATUSES: RunStatus[] = [
  "succeeded",
  "cancelled",
  "failed",
  "interrupted",
];
