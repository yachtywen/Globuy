import { useCallback, useEffect, useReducer, useRef } from "react";
import { ApiClientError, requestId, sessionApi, websocketUrl } from "./api";
import { initialState, workbenchReducer } from "./state";
import type { MonitorEvent, ThreadDetail, ThreadSummary } from "./types";
import { TERMINAL_RUN_STATUSES } from "./types";

const ACTIVE_STORAGE_KEY = "globuy.active_thread_id";

function messageFrom(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败，请稍后重试";
}

function asSummary(thread: Partial<ThreadSummary> & Pick<ThreadSummary, "thread_id" | "title" | "status" | "created_at">): ThreadSummary {
  return {
    updated_at: thread.created_at,
    archived_at: null,
    ...thread,
  };
}

export function useGlobuyTask() {
  const [state, dispatch] = useReducer(workbenchReducer, initialState);
  const stateRef = useRef(state);
  const socketRef = useRef<WebSocket | null>(null);
  const subscriptionRef = useRef<{ threadId: string; runId: string } | null>(null);
  const lastSequenceRef = useRef(0);
  const lastPacketAtRef = useRef(Date.now());
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const intentionalCloseRef = useRef(false);
  const terminalRef = useRef(false);
  const initializedRef = useRef(false);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  const closeSocket = useCallback(() => {
    intentionalCloseRef.current = true;
    if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
    reconnectTimerRef.current = null;
    socketRef.current?.close();
    socketRef.current = null;
    subscriptionRef.current = null;
    dispatch({ type: "CONNECTION", status: "idle" });
  }, []);

  const syncRun = useCallback(async (threadId: string, runId: string, refreshMessages = false) => {
    try {
      const run = await sessionApi.getRun(threadId, runId);
      dispatch({
        type: "RUN_SYNC",
        status: run.status,
        result: run.result,
        artifacts: run.artifacts,
        error: run.error?.message,
      });
      if (run.last_sequence > lastSequenceRef.current) lastSequenceRef.current = run.last_sequence;
      if (TERMINAL_RUN_STATUSES.includes(run.status)) terminalRef.current = true;
      if (refreshMessages) {
        const detail = await sessionApi.getThread(threadId);
        if (stateRef.current.viewingThreadId === threadId && stateRef.current.viewMode === "active") {
          dispatch({ type: "MERGE_DETAIL", detail });
        }
      }
      return run;
    } catch (error) {
      dispatch({ type: "ERROR", message: messageFrom(error) });
      return null;
    }
  }, []);

  const connectRef = useRef<(threadId: string, runId: string, after?: number, reconnecting?: boolean) => void>(() => undefined);
  connectRef.current = (threadId, runId, after = lastSequenceRef.current, reconnecting = false) => {
    intentionalCloseRef.current = true;
    socketRef.current?.close();
    intentionalCloseRef.current = false;
    terminalRef.current = false;
    subscriptionRef.current = { threadId, runId };
    dispatch({ type: "CONNECTION", status: reconnecting ? "reconnecting" : "connecting" });
    const socket = new WebSocket(websocketUrl(threadId, runId, after));
    socketRef.current = socket;

    socket.onopen = () => {
      if (socketRef.current !== socket) return;
      reconnectAttemptRef.current = 0;
      lastPacketAtRef.current = Date.now();
      dispatch({ type: "CONNECTION", status: "connected" });
    };
    socket.onmessage = (packet) => {
      lastPacketAtRef.current = Date.now();
      let event: MonitorEvent;
      try {
        event = JSON.parse(packet.data) as MonitorEvent;
      } catch {
        return;
      }
      const activeSubscription = subscriptionRef.current;
      if (
        event.type !== "monitor_event" ||
        event.thread_id !== activeSubscription?.threadId ||
        event.run_id !== activeSubscription.runId ||
        stateRef.current.viewMode !== "active"
      ) return;

      if (event.event === "CUSTOM") {
        const name = event.data.name;
        if (name === "stream_ready" || name === "heartbeat") {
          dispatch({ type: "CONNECTION", status: "connected" });
          return;
        }
        if (name === "replay_gap") {
          void syncRun(threadId, runId, true);
          return;
        }
      }
      if (event.sequence !== null) {
        if (event.sequence <= lastSequenceRef.current) return;
        lastSequenceRef.current = event.sequence;
      }
      dispatch({ type: "EVENT", event });
      if (["RUN_FINISHED", "RUN_ERROR", "TASK_CANCELLED"].includes(event.event)) {
        terminalRef.current = true;
        intentionalCloseRef.current = true;
        socket.close();
        void syncRun(threadId, runId, true);
      }
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      if (socketRef.current === socket) socketRef.current = null;
      if (intentionalCloseRef.current || terminalRef.current) return;
      const current = stateRef.current;
      if (
        current.viewMode !== "active" ||
        current.viewingThreadId !== threadId ||
        subscriptionRef.current?.runId !== runId
      ) return;
      const attempt = reconnectAttemptRef.current++;
      const seconds = [1, 2, 4, 8, 15][Math.min(attempt, 4)];
      const delay = seconds * 1000 * (0.85 + Math.random() * 0.3);
      dispatch({ type: "CONNECTION", status: "reconnecting" });
      reconnectTimerRef.current = window.setTimeout(
        () => connectRef.current(threadId, runId, lastSequenceRef.current, true),
        delay,
      );
    };
  };

  const refreshThreadLists = useCallback(async () => {
    const [activePage, archivePage] = await Promise.all([
      sessionApi.listThreads("active"),
      sessionApi.listThreads("archived"),
    ]);
    dispatch({
      type: "SET_THREADS",
      active: activePage.items[0] ?? null,
      archived: archivePage.items,
      cursor: archivePage.next_cursor,
    });
    return activePage.items[0] ?? null;
  }, []);

  const openActiveDetail = useCallback(async (threadId: string) => {
    const detail = await sessionApi.getThread(threadId);
    closeSocket();
    dispatch({ type: "OPEN_DETAIL", detail, mode: "active" });
    localStorage.setItem(ACTIVE_STORAGE_KEY, threadId);
    const run = detail.runs.at(-1);
    if (run && !TERMINAL_RUN_STATUSES.includes(run.status)) {
      lastSequenceRef.current = 0;
      dispatch({ type: "TASK_STATUS", status: run.status });
      connectRef.current(threadId, run.run_id, 0);
    }
  }, [closeSocket]);

  const bootstrap = useCallback(async () => {
    dispatch({ type: "LOAD_START" });
    try {
      let active = await refreshThreadLists();
      if (!active) {
        const created = await sessionApi.createThread(null, requestId("thread"));
        active = asSummary(created);
        await refreshThreadLists();
      }
      await openActiveDetail(active.thread_id);
    } catch (error) {
      dispatch({ type: "ERROR", message: messageFrom(error) });
    }
  }, [openActiveDetail, refreshThreadLists]);

  useEffect(() => {
    if (!initializedRef.current) {
      initializedRef.current = true;
      void bootstrap();
    }
    const watchdog = window.setInterval(() => {
      if (socketRef.current?.readyState === WebSocket.OPEN && Date.now() - lastPacketAtRef.current > 45_000) {
        socketRef.current.close();
      }
    }, 5_000);
    const onFocus = () => {
      void (async () => {
        try {
          const active = await refreshThreadLists();
          if (active && active.thread_id !== stateRef.current.activeThreadId && stateRef.current.viewMode === "active") {
            await openActiveDetail(active.thread_id);
          }
        } catch (error) {
          dispatch({ type: "ERROR", message: messageFrom(error) });
        }
      })();
    };
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(watchdog);
      window.removeEventListener("focus", onFocus);
      closeSocket();
    };
  }, [bootstrap, closeSocket, openActiveDetail, refreshThreadLists]);

  const loadMoreArchivedThreads = useCallback(async () => {
    if (!stateRef.current.archiveCursor) return;
    try {
      const page = await sessionApi.listThreads("archived", stateRef.current.archiveCursor);
      dispatch({ type: "APPEND_THREADS", archived: page.items, cursor: page.next_cursor });
    } catch (error) {
      dispatch({ type: "ERROR", message: messageFrom(error) });
    }
  }, []);

  const openArchivedThread = useCallback(async (threadId: string) => {
    dispatch({ type: "LOAD_START" });
    closeSocket();
    try {
      const detail = await sessionApi.getThread(threadId);
      dispatch({ type: "OPEN_DETAIL", detail, mode: "archived" });
    } catch (error) {
      dispatch({ type: "ERROR", message: messageFrom(error) });
    }
  }, [closeSocket]);

  const returnToActiveThread = useCallback(async () => {
    const activeId = stateRef.current.activeThreadId || localStorage.getItem(ACTIVE_STORAGE_KEY);
    if (!activeId) return void bootstrap();
    dispatch({ type: "LOAD_START" });
    try {
      await openActiveDetail(activeId);
    } catch (error) {
      dispatch({ type: "ERROR", message: messageFrom(error) });
    }
  }, [bootstrap, openActiveDetail]);

  const createNewThread = useCallback(async () => {
    dispatch({ type: "ARCHIVE_START" });
    try {
      const created = await sessionApi.createThread(
        stateRef.current.activeThreadId,
        requestId("thread"),
      );
      closeSocket();
      localStorage.setItem(ACTIVE_STORAGE_KEY, created.thread_id);
      await refreshThreadLists();
      await openActiveDetail(created.thread_id);
      dispatch({ type: "ARCHIVE_END" });
      return created.thread_id;
    } catch (error) {
      if (error instanceof ApiClientError && error.code === "ACTIVE_THREAD_CHANGED") await bootstrap();
      dispatch({ type: "ERROR", message: messageFrom(error) });
      return null;
    }
  }, [bootstrap, closeSocket, openActiveDetail, refreshThreadLists]);

  const startTask = useCallback(async (query: string) => {
    const current = stateRef.current;
    if (!current.activeThreadId || current.viewMode !== "active") return;
    try {
      const task = await sessionApi.createTask(query, current.activeThreadId, requestId("run"));
      lastSequenceRef.current = 0;
      dispatch({ type: "TASK_ACCEPTED", runId: task.run_id, query, createdAt: task.created_at });
      connectRef.current(task.thread_id, task.run_id, 0);
      void refreshThreadLists();
    } catch (error) {
      dispatch({ type: "ERROR", message: messageFrom(error) });
    }
  }, [refreshThreadLists]);

  const cancelTask = useCallback(async () => {
    const { activeThreadId, currentRunId } = stateRef.current;
    if (!activeThreadId || !currentRunId) return;
    dispatch({ type: "TASK_STATUS", status: "cancelling" });
    try {
      const response = await sessionApi.cancelRun(activeThreadId, currentRunId);
      if (response.terminal) await syncRun(activeThreadId, currentRunId, true);
    } catch (error) {
      dispatch({ type: "ERROR", message: messageFrom(error) });
      await syncRun(activeThreadId, currentRunId);
    }
  }, [syncRun]);

  const reconnectNow = useCallback(() => {
    const subscription = subscriptionRef.current;
    if (!subscription || terminalRef.current) return;
    if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
    connectRef.current(subscription.threadId, subscription.runId, lastSequenceRef.current, true);
  }, []);

  return {
    state,
    loadThreads: refreshThreadLists,
    loadMoreArchivedThreads,
    openArchivedThread,
    returnToActiveThread,
    createNewThread,
    startTask,
    cancelTask,
    reconnectNow,
    clearError: () => dispatch({ type: "ERROR", message: null }),
  };
}
