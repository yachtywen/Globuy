import type { MemoryCandidate, MemoryEntry, PriceHistory, RunStatusResponse, ThreadDetail, ThreadSummary, Wishlist } from "./types";

const API_ROOT = "/api/v1";

export class ApiClientError extends Error {
  constructor(
    message: string,
    public readonly code = "REQUEST_FAILED",
    public readonly status = 0,
    public readonly retryable = false,
    public readonly details: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiClientError";
  }
}

type RequestOptions = RequestInit & { timeoutMs?: number; csrf?: boolean };

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { timeoutMs = 15_000, csrf = true, ...init } = options;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  let response: Response;
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      ...init,
      signal: init.signal || controller.signal,
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(!csrf || !init.method || ["GET", "HEAD", "OPTIONS"].includes(init.method.toUpperCase())
          ? {}
          : { "X-CSRF-Token": readCookie("globuy_csrf") || "" }),
        ...init.headers,
      },
    });
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "AbortError";
    throw new ApiClientError(timedOut ? "请求超时，请稍后重试" : "无法连接后端服务，请确认 FastAPI 已启动", timedOut ? "REQUEST_TIMEOUT" : "NETWORK_ERROR", 0, true);
  } finally {
    window.clearTimeout(timeout);
  }
  if (response.status === 204) return undefined as T;
  const body = (await response.json().catch(() => null)) as
    | { error?: { code?: string; message?: string; retryable?: boolean; details?: Record<string, unknown> } }
    | null;
  if (!response.ok) {
    const error = new ApiClientError(
      body?.error?.message || `请求失败（${response.status}）`,
      body?.error?.code,
      response.status,
      body?.error?.retryable,
      body?.error?.details,
    );
    if (response.status === 401 && error.code === "AUTH_REQUIRED") window.dispatchEvent(new CustomEvent("globuy:auth-required"));
    throw error;
  }
  return body as T;
}

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  const value = document.cookie.split("; ").find((item) => item.startsWith(prefix));
  return value ? decodeURIComponent(value.slice(prefix.length)) : null;
}

export type AuthUser = { user_id: string; email: string; display_name: string };

export const authApi = {
  register(email: string, password: string, displayName: string, idempotencyKey: string) {
    return request<{ user: AuthUser; csrf_token: string }>("/auth/register", {
      method: "POST",
      csrf: false,
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ email, password, display_name: displayName }),
    });
  },
  login(email: string, password: string) {
    return request<{ user: AuthUser; csrf_token: string }>("/auth/login", {
      method: "POST",
      csrf: false,
      body: JSON.stringify({ email, password }),
    });
  },
  me() {
    return request<{ user: AuthUser }>("/auth/me");
  },
  logout() {
    return request<void>("/auth/logout", { method: "POST" });
  },
};

export const sessionApi = {
  listThreads(status: "active" | "archived", cursor?: string | null) {
    const query = new URLSearchParams({ status, limit: "20" });
    if (cursor) query.set("cursor", cursor);
    return request<{ items: ThreadSummary[]; next_cursor: string | null }>(`/threads?${query}`);
  },
  getThread(threadId: string) {
    return request<ThreadDetail>(`/threads/${encodeURIComponent(threadId)}`);
  },
  createThread(currentThreadId: string | null, clientRequestId: string) {
    return request<ThreadSummary & { archived_thread_id: string | null; archived_run_id: string | null }>(
      "/threads",
      {
        method: "POST",
        body: JSON.stringify({
          current_thread_id: currentThreadId,
          client_request_id: clientRequestId,
        }),
      },
    );
  },
  createTask(query: string, threadId: string, clientRequestId: string) {
    return request<{
      status: "starting";
      thread_id: string;
      run_id: string;
      created_at: string;
      ws_url: string;
      status_url: string;
      replaced_run_id: string | null;
    }>("/tasks", {
      method: "POST",
      body: JSON.stringify({ query, thread_id: threadId, client_request_id: clientRequestId }),
    });
  },
  getRun(threadId: string, runId: string) {
    return request<RunStatusResponse>(
      `/threads/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}`,
    );
  },
  cancelRun(threadId: string, runId: string) {
    return request<{ status: RunStatusResponse["status"]; terminal: boolean }>(
      `/threads/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/cancel`,
      { method: "POST" },
    );
  },
};

export const wishlistApi = {
  getDefault() {
    return request<Wishlist>("/wishlists/default");
  },
  add(offerId: string, sourceThreadId: string | null, sourceRunId: string | null, clientRequestId = requestId("wishlist")) {
    return request<Wishlist["items"][number]>("/wishlists/default/items", {
      method: "POST",
      body: JSON.stringify({
        offer_id: offerId,
        source_thread_id: sourceThreadId,
        source_run_id: sourceRunId,
        client_request_id: clientRequestId,
      }),
    });
  },
  remove(itemId: string) {
    return request<void>(`/wishlists/default/items/${encodeURIComponent(itemId)}`, {
      method: "DELETE",
    });
  },
  update(itemId: string, changes: { status?: "active" | "removed" | "purchased"; target_price?: number | null; note?: string | null }) {
    return request<Wishlist["items"][number]>(`/wishlists/default/items/${encodeURIComponent(itemId)}`, {
      method: "PATCH",
      body: JSON.stringify(changes),
    });
  },
  priceHistory(itemId: string) {
    return request<PriceHistory>(`/wishlists/default/items/${encodeURIComponent(itemId)}/price-history`);
  },
};

export const memoryApi = {
  list(status: "active" | "archived" = "active") {
    return request<{ items: MemoryEntry[] }>(`/memories?status=${status}`);
  },
  create(category: MemoryEntry["category"], key: string, content: string) {
    return request<MemoryEntry>("/memories", {
      method: "POST",
      body: JSON.stringify({ category, key, content, confidence: 1 }),
    });
  },
  remove(memoryId: string) {
    return request<void>(`/memories/${encodeURIComponent(memoryId)}`, { method: "DELETE" });
  },
  restore(memoryId: string) {
    return request<MemoryEntry>(`/memories/${encodeURIComponent(memoryId)}/restore`, {
      method: "POST",
    });
  },
  candidates() {
    return request<{ items: MemoryCandidate[] }>("/memory-candidates?status=pending");
  },
  confirmCandidate(candidateId: string) {
    return request<MemoryEntry>(`/memory-candidates/${encodeURIComponent(candidateId)}/confirm`, {
      method: "POST",
      body: JSON.stringify({}),
    });
  },
  rejectCandidate(candidateId: string) {
    return request<void>(`/memory-candidates/${encodeURIComponent(candidateId)}/reject`, {
      method: "POST",
    });
  },
};

export const systemApi = {
  async health() {
    let response: Response;
    try {
      response = await fetch("/healthz");
    } catch {
      throw new ApiClientError("无法连接后端服务", "NETWORK_ERROR", 0, true);
    }
    if (!response.ok) throw new ApiClientError("后端服务暂不可用", "SERVICE_UNAVAILABLE", response.status, true);
    return response.json() as Promise<{ status: string; model_provider: string; database?: string }>;
  },
};

export function websocketUrl(threadId: string, runId: string, after: number): string {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}${API_ROOT}/ws/${encodeURIComponent(threadId)}?run_id=${encodeURIComponent(runId)}&after=${after}`;
}

export function requestId(prefix: string): string {
  return `${prefix}_${crypto.randomUUID()}`;
}
