import {
  Pulse,
  ArrowClockwise,
  ArrowLeft,
  CaretRight,
  Check,
  ClockCounterClockwise,
  DownloadSimple,
  House,
  Heart,
  MagnifyingGlass,
  PaperPlaneTilt,
  Plus,
  Stop,
  SignOut,
  UserCircle,
  X,
} from "@phosphor-icons/react";
import { FormEvent, KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { LandingPage } from "./LandingPage";
import { AccountPage } from "./AccountPage";
import { AuthPage } from "./AuthPage";
import { WishlistPage } from "./WishlistPage";
import { ProductResults } from "./ProductResults";
import { authApi, type AuthUser } from "./api";
import { sanitizeShoppingMarkdown } from "./presentation";
import brandMark from "./assets/globuy-mark.webp";
import heroIllustration from "./assets/globuy-hero.webp";
import { useGlobuyTask } from "./useGlobuyTask";
import type { ChatMessage, ThreadSummary } from "./types";

const STATUS_LABELS: Record<string, string> = {
  idle: "待命",
  starting: "正在启动",
  running: "运行中",
  cancelling: "正在取消",
  succeeded: "已完成",
  cancelled: "已取消",
  failed: "失败",
  interrupted: "服务中断",
  connecting: "连接中",
  connected: "事件流在线",
  reconnecting: "正在重连",
  offline: "连接中断",
};

const PHASE_LABELS: Record<string, string> = {
  think: "理解需求",
  act: "执行检索",
  observe: "整理证据",
  reflect: "复核结果",
  summarize: "生成建议",
};

const TOOL_LABELS: Record<string, string> = {
  web_search: "网络搜索",
  item_search: "商品检索",
  category_insight: "品类洞察",
  preference_memory: "偏好读取",
};

const ICONS = {
  plus: Plus,
  history: ClockCounterClockwise,
  activity: Pulse,
  close: X,
  arrow: ArrowLeft,
  send: PaperPlaneTilt,
  stop: Stop,
  download: DownloadSimple,
  reconnect: ArrowClockwise,
  chevron: CaretRight,
  home: House,
  search: MagnifyingGlass,
};

function Icon({ name, size = 18 }: { name: keyof typeof ICONS; size?: number }) {
  const Component = ICONS[name];
  return <Component aria-hidden="true" className="icon" size={size} weight="regular" />;
}

function formatDate(value?: string | null, full = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", full
    ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
    : { month: "numeric", day: "numeric" }).format(date);
}

function statusTone(status?: string | null) {
  if (status === "succeeded") return "success";
  if (["running", "starting", "cancelling"].includes(status || "")) return "running";
  if (["failed", "cancelled", "interrupted"].includes(status || "")) return "warning";
  return "neutral";
}

function HistoryPanel({
  threads,
  activeId,
  viewingId,
  hasMore,
  onOpen,
  onActive,
  onMore,
  onClose,
}: {
  threads: ThreadSummary[];
  activeId: string | null;
  viewingId: string | null;
  hasMore: boolean;
  onOpen: (id: string) => void;
  onActive: () => void;
  onMore: () => void;
  onClose: () => void;
}) {
  const [search, setSearch] = useState("");
  const active = threads.find((item) => item.status === "active");
  const archived = threads.filter((item) => item.status === "archived" && item.title.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  return (
    <div className="panel-content history-content">
      <div className="history-mobile-close mobile-panel-heading">
        <button aria-label="关闭历史会话" className="icon-button" onClick={onClose}><Icon name="close" /></button>
      </div>
      <label className="history-search"><span className="sr-only">搜索会话</span><Icon name="search" size={16} /><input onChange={(event) => setSearch(event.target.value)} placeholder="搜索本页归档" type="search" value={search} /></label>
      <section className="session-section" aria-labelledby="active-session-heading">
        <h3 id="active-session-heading">当前会话</h3>
        {active ? (
          <button
            aria-current={viewingId === active.thread_id ? "page" : undefined}
            className={`session-item ${viewingId === active.thread_id ? "selected" : ""}`}
            onClick={onActive}
          >
            <span className="session-marker active-marker" />
            <span className="session-copy"><strong>{active.title}</strong><small>可继续对话</small></span>
            <Icon name="chevron" size={16} />
          </button>
        ) : <p className="panel-empty">正在建立新会话…</p>}
      </section>
      <section className="session-section archive-list" aria-labelledby="archive-session-heading">
        <div className="section-row"><h3 id="archive-session-heading">历史归档</h3><span>{archived.length}</span></div>
        {archived.length === 0 && <p className="panel-empty">{search ? "没有匹配的归档会话。" : "完成一次对话后，归档会显示在这里。"}</p>}
        {archived.map((thread) => (
          <button
            aria-current={viewingId === thread.thread_id ? "page" : undefined}
            className={`session-item archive-item ${viewingId === thread.thread_id ? "selected" : ""}`}
            key={thread.thread_id}
            onClick={() => onOpen(thread.thread_id)}
          >
            <span className={`session-marker ${statusTone(thread.last_run_status)}`} />
            <span className="session-copy">
              <strong>{thread.title}</strong>
              <small>{formatDate(thread.archived_at, true)} · {STATUS_LABELS[thread.last_run_status || ""] || "已归档"}</small>
            </span>
          </button>
        ))}
        {hasMore && <button className="text-button load-more" onClick={onMore}>加载更多</button>}
      </section>
      {activeId && <span className="sr-only">活动会话 {activeId}</span>}
    </div>
  );
}

export function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      components={{
        a: ({ href, children: linkChildren }) => (
          <a href={href} rel="noopener noreferrer" target="_blank">{linkChildren}</a>
        ),
      }}
      remarkPlugins={[remarkGfm]}
    >
      {children}
    </ReactMarkdown>
  );
}

function MessageBubble({ message, placeholder }: { message: ChatMessage; placeholder?: string }) {
  const content = message.role === "assistant"
    ? sanitizeShoppingMarkdown(message.content)
    : message.content;
  return (
    <article className={`message ${message.role}`}>
      {message.role === "assistant" && <img alt="Globuy Agent" className="message-avatar" height="34" src={brandMark} width="34" />}
      <div className="message-meta">
        <strong>{message.role === "user" ? "你" : message.role === "assistant" ? "GLOBUY" : "系统"}</strong>
        <time dateTime={message.created_at}>{formatDate(message.created_at, true)}</time>
      </div>
      <div className="markdown-body"><Markdown>{content || (message.streaming ? (placeholder || "正在组织建议…") : "")}</Markdown></div>
      {message.streaming && <span className="streaming-caret" aria-label="正在生成" />}
      {message.is_partial && <small className="partial-label">回答未完整生成</small>}
    </article>
  );
}

function RuntimePanel({ state, onReconnect, onClose }: {
  state: ReturnType<typeof useGlobuyTask>["state"];
  onReconnect: () => void;
  onClose: () => void;
}) {
  const runs = state.detail?.runs ?? [];
  const completedSteps = state.steps.filter((step) => step.status === "succeeded").length;
  return (
    <div className="panel-content runtime-content">
      <div className="panel-heading mobile-panel-heading">
        <div><span className="section-label">LIVE TRACE</span><h2>{state.viewMode === "archived" ? "运行摘要" : "运行轨迹"}</h2></div>
        <button aria-label="关闭运行轨迹" className="icon-button" onClick={onClose}><Icon name="close" /></button>
      </div>
      {state.viewMode === "archived" ? (
        <div className="run-summaries">
          <p className="panel-intro">归档会话只保留运行终态与结果，不伪造临时过程事件。</p>
          {runs.map((run, index) => (
            <article className="run-summary" key={run.run_id}>
              <div><span>RUN {String(index + 1).padStart(2, "0")}</span><span className={`status-dot ${statusTone(run.status)}`} /></div>
              <strong>{STATUS_LABELS[run.status]}</strong>
              <p>{run.query}</p>
              <time>{formatDate(run.finished_at || run.created_at, true)}</time>
              {run.error_message && <small className="warning-text">{run.error_message}</small>}
            </article>
          ))}
          {!runs.length && <p className="panel-empty">这个会话没有运行记录。</p>}
        </div>
      ) : (
        <>
          <div className="live-status-block">
            <div className="live-status-row"><span className={`pulse-dot ${statusTone(state.taskStatus)}`} /><strong>{STATUS_LABELS[state.taskStatus]}</strong></div>
            {state.initializationMessage && <p aria-live="polite" className="initialization-message">{state.initializationMessage}</p>}
            <div className="connection-row">
              <span>{STATUS_LABELS[state.connectionStatus] || state.connectionStatus}</span>
              <span>{completedSteps} / {Math.max(state.steps.length, completedSteps)} 阶段完成</span>
              {state.connectionStatus === "reconnecting" && <button className="text-button" onClick={onReconnect}><Icon name="reconnect" size={14} />立即重连</button>}
            </div>
          </div>
          <section className="trace-section"><h3>阶段</h3>
            {state.steps.length ? <ol className="step-list">{state.steps.map((step, index) => (
              <li className={step.status} key={step.id}><span>{step.status === "succeeded" ? <Check size={10} weight="bold" /> : String(index).padStart(2, "0")}</span><div><strong>{PHASE_LABELS[step.phase] || step.phase}</strong><small>迭代 {step.iteration + 1} · {STATUS_LABELS[step.status] || step.status}</small></div></li>
            ))}</ol> : <p className="panel-empty">任务开始后，这里会显示决策阶段。</p>}
          </section>
          {state.toolCalls.length > 0 && <section className="trace-section"><h3>工具调用</h3>
            <div className="tool-list">{state.toolCalls.map((tool) => (
              <article key={tool.id}><span className={`tool-indicator ${tool.status}`} /><div><strong>{TOOL_LABELS[tool.name] || tool.name}</strong><small>{tool.status === "running" ? "执行中" : tool.status === "failed" ? "调用失败" : `${tool.durationMs ?? 0} ms`}</small></div></article>
            ))}</div>
          </section>}
          {state.forks.length > 0 && <section className="trace-section"><h3>并行分支</h3>
            <div className="fork-list">{state.forks.map((fork) => <article key={fork.id}><strong>{fork.reason}</strong><small>{fork.toolNames.join(" · ") || "独立分析"}</small></article>)}</div>
          </section>}
        </>
      )}
    </div>
  );
}

export function NewThreadDialog({ open, busy, submitting, onCancel, onConfirm }: {
  open: boolean;
  busy: boolean;
  submitting: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    cancelRef.current?.focus();
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && !submitting) onCancel();
      if (event.key !== "Tab") return;
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled)");
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onCancel, submitting]);
  if (!open) return null;
  return (
    <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && !submitting && onCancel()}>
      <div aria-describedby="new-thread-description" aria-labelledby="new-thread-title" aria-modal="true" className="modal" ref={dialogRef} role="dialog">
        <span className="section-label">NEW SESSION</span>
        <h2 id="new-thread-title">开启新对话？</h2>
        <p id="new-thread-description">开启后，当前对话将自动归档。归档内容只能查看，不能继续发送消息。</p>
        {busy && <p className="modal-warning">当前任务仍在运行。继续后将先取消该任务，再归档当前对话。</p>}
        <div className="modal-actions">
          <button className="secondary-button" disabled={submitting} onClick={onCancel} ref={cancelRef}>取消</button>
          <button className="archive-button" disabled={submitting} onClick={onConfirm}>{submitting ? "正在归档…" : "归档并开启新对话"}</button>
        </div>
      </div>
    </div>
  );
}

function Composer({ disabled, busy, onSend, onCancel }: {
  disabled: boolean;
  busy: boolean;
  onSend: (query: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState("");
  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const query = value.trim();
    if (!query || disabled || busy) return;
    onSend(query);
    setValue("");
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };
  const examples = ["推荐适合通勤的降噪耳机", "适合程序员的机械键盘", "帮我比较宿舍使用的显示器"];
  return (
    <div className="composer-wrap">
      {!value && !busy && <div className="prompt-suggestions">{examples.map((example) => <button key={example} onClick={() => setValue(example)}>{example}</button>)}</div>}
      <form className="composer" onSubmit={submit}>
        <label className="sr-only" htmlFor="shopping-query">描述你的购物需求</label>
        <textarea
          disabled={disabled || busy}
          id="shopping-query"
          maxLength={20000}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={onKeyDown}
          onInput={(event) => {
            const target = event.currentTarget;
            target.style.height = "auto";
            target.style.height = `${Math.min(target.scrollHeight, 180)}px`;
          }}
          placeholder="描述预算、用途和偏好，我会整理一份可核验的建议…"
          rows={3}
          value={value}
        />
        <div className="composer-footer">
          <span>{value.length ? `${value.length} / 20000` : "Enter 发送 · Shift + Enter 换行"}</span>
          {busy ? (
            <button aria-label="取消当前任务" className="cancel-task-button" onClick={onCancel} type="button"><Icon name="stop" />取消任务</button>
          ) : (
            <div className="composer-actions">{value && <button aria-label="清空输入" className="clear-input-button" onClick={() => setValue("")} type="button"><Icon name="close" size={15} /></button>}<button aria-label="发送购物需求" className="send-button" disabled={disabled || !value.trim()} type="submit"><Icon name="send" />发送</button></div>
          )}
        </div>
      </form>
    </div>
  );
}

export function useConversationAutoScroll(
  target: { current: HTMLElement | null },
  messages: readonly ChatMessage[],
) {
  useEffect(() => {
    target.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, target]);
}

function WorkbenchPage({ navigate, routeThreadId, user, onLogout }: {
  navigate: (path: string, replace?: boolean) => void;
  routeThreadId: string | null;
  user: AuthUser;
  onLogout: () => void;
}) {
  const task = useGlobuyTask();
  const { state } = task;
  const [historyOpen, setHistoryOpen] = useState(false);
  const [runtimeOpen, setRuntimeOpen] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const conversationEnd = useRef<HTMLDivElement>(null);
  const attemptedRoute = useRef<string | null>(null);
  const isBusy = ["starting", "running", "cancelling"].includes(state.taskStatus);
  useConversationAutoScroll(conversationEnd, state.messages);

  useEffect(() => {
    if (state.threadLoading) return;
    if (!routeThreadId && state.viewingThreadId) {
      navigate(`/assistant/${encodeURIComponent(state.viewingThreadId)}`, true);
      return;
    }
    if (!routeThreadId || routeThreadId === state.viewingThreadId || attemptedRoute.current === routeThreadId) return;
    attemptedRoute.current = routeThreadId;
    if (routeThreadId === state.activeThreadId) void task.returnToActiveThread();
    else void task.openArchivedThread(routeThreadId);
  }, [navigate, routeThreadId, state.activeThreadId, state.threadLoading, state.viewingThreadId, task]);

  const confirmNewThread = async () => {
    const threadId = await task.createNewThread();
    if (threadId) navigate(`/assistant/${encodeURIComponent(threadId)}`);
    setDialogOpen(false);
  };
  const viewingTitle = state.detail?.title || "新对话";
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand"><img alt="" className="brand-mark" height="42" src={brandMark} width="42" /><div><strong>GLOBUY</strong><small>SHOPPING INTELLIGENCE</small></div></div>
        <div className="topbar-actions">
          <button aria-label="返回品牌首页" className="icon-button desktop-home" onClick={() => navigate("/")}><Icon name="home" /></button>
          <button aria-label="打开心愿库" className="wishlist-nav-button" onClick={() => navigate("/wishlist")}><Heart size={18} /><span>心愿库</span></button>
          <button aria-label="打开个人中心" className="icon-button" onClick={() => navigate("/account")} title={user.display_name}><UserCircle size={19} /></button>
          <button aria-label="退出登录" className="icon-button desktop-logout" onClick={onLogout} title="退出登录"><SignOut size={18} /></button>
          <button aria-label="打开历史会话" className="icon-button mobile-only" onClick={() => setHistoryOpen(true)}><Icon name="history" /></button>
          <button aria-label="打开运行轨迹" className="icon-button tablet-runtime-button" onClick={() => setRuntimeOpen(true)}><Icon name="activity" /></button>
          <span className={`header-status ${statusTone(state.taskStatus)}`}><i />{STATUS_LABELS[state.taskStatus]}</span>
          <button aria-label="开启新对话" className="top-new-button" onClick={() => setDialogOpen(true)}><Icon name="plus" />新对话</button>
        </div>
      </header>

      <div className="workspace">
        {(historyOpen || runtimeOpen) && <button aria-label="关闭侧栏" className="drawer-scrim" onClick={() => { setHistoryOpen(false); setRuntimeOpen(false); }} />}
        <aside aria-label="历史会话" className={`left-panel ${historyOpen ? "drawer-open" : ""}`}>
          <HistoryPanel
            activeId={state.activeThreadId}
            hasMore={Boolean(state.archiveCursor)}
            onActive={() => { if (state.activeThreadId) navigate(`/assistant/${encodeURIComponent(state.activeThreadId)}`); void task.returnToActiveThread(); setHistoryOpen(false); }}
            onClose={() => setHistoryOpen(false)}
            onMore={() => void task.loadMoreArchivedThreads()}
            onOpen={(id) => { navigate(`/assistant/${encodeURIComponent(id)}`); void task.openArchivedThread(id); setHistoryOpen(false); }}
            threads={state.threads}
            viewingId={state.viewingThreadId}
          />
        </aside>

        <main className="conversation-panel">
          {state.viewMode === "archived" && (
            <div className="conversation-header">
              <div>
                <div className="conversation-title-row"><h1>{viewingTitle}</h1><span className="archive-badge">已归档 · 只读</span></div>
                <p>归档于 {formatDate(state.detail?.archived_at, true)}</p>
              </div>
              <button className="return-button" onClick={() => { if (state.activeThreadId) navigate(`/assistant/${encodeURIComponent(state.activeThreadId)}`); void task.returnToActiveThread(); }}><Icon name="arrow" />返回当前会话</button>
            </div>
          )}

          {state.error && <div className="error-banner" role="alert"><span>{state.error}</span><button aria-label="关闭错误提示" onClick={task.clearError}><Icon name="close" size={16} /></button></div>}
          <section aria-busy={state.threadLoading} aria-label="购物对话" className="conversation-scroll">
            {state.threadLoading ? (
              <div className="loading-state"><span /><span /><span /><p>正在读取会话</p></div>
            ) : state.messages.length === 0 ? (
              <div className="empty-state">
                <img alt="彩铅绘制的地球与购物车" className="empty-illustration" height="213" src={heroIllustration} width="320" />
                <span className="empty-index">01 / DISCOVER</span>
                <h2>今天想买什么？</h2>
                <p>告诉我预算、用途和偏好，Globuy 会帮你搜索并比较。</p>
                <div className="capability-row"><span>需求拆解</span><span>并行检索</span><span>结果复核</span></div>
              </div>
            ) : (
              <div className="message-list">{state.messages.map((message) => <MessageBubble key={message.message_id} message={message} placeholder={message.streaming ? state.initializationMessage ?? undefined : undefined} />)}<div ref={conversationEnd} /></div>
            )}
            <ProductResults artifacts={state.artifacts} result={state.result} sourceRunId={state.currentRunId} sourceThreadId={state.viewingThreadId} />
          </section>
          {state.viewMode === "active" ? (
            <Composer disabled={state.threadLoading || state.archiveCreating} busy={isBusy} onCancel={() => void task.cancelTask()} onSend={(query) => void task.startTask(query)} />
          ) : <div className="readonly-footer"><span>此会话已归档，内容保持只读。</span><button onClick={() => { if (state.activeThreadId) navigate(`/assistant/${encodeURIComponent(state.activeThreadId)}`); void task.returnToActiveThread(); }}>返回当前会话</button></div>}
        </main>

        <aside aria-label="运行轨迹" className={`right-panel ${runtimeOpen ? "drawer-open" : ""}`}>
          <RuntimePanel onClose={() => setRuntimeOpen(false)} onReconnect={task.reconnectNow} state={state} />
        </aside>
      </div>
      <NewThreadDialog
        busy={isBusy}
        onCancel={() => setDialogOpen(false)}
        onConfirm={() => void confirmNewThread()}
        open={dialogOpen}
        submitting={state.archiveCreating}
      />
    </div>
  );
}

function routeFromPath(pathname: string) {
  const match = pathname.match(/^\/assistant\/([A-Za-z0-9_-]{1,128})\/?$/);
  return { assistant: pathname === "/assistant" || Boolean(match), threadId: match?.[1] ?? null };
}

export default function App() {
  const [pathname, setPathname] = useState(() => window.location.pathname);
  const [user, setUser] = useState<AuthUser | null | undefined>(undefined);
  useEffect(() => {
    const update = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  const [authError, setAuthError] = useState<string | null>(null);
  const restoreAuth = useCallback(() => {
    setAuthError(null);
    setUser(undefined);
    let active = true;
    void authApi.me().then((response) => { if (active) setUser(response.user); }).catch((error) => {
      if (!active) return;
      if (error instanceof Error && "status" in error && (error as { status: number }).status === 401) setUser(null);
      else { setAuthError(error instanceof Error ? error.message : "登录状态检查失败"); setUser(undefined); }
    });
    return () => { active = false; };
  }, []);
  useEffect(() => restoreAuth(), [restoreAuth]);
  useEffect(() => {
    const onAuthRequired = () => { setUser(null); setAuthError(null); };
    window.addEventListener("globuy:auth-required", onAuthRequired);
    return () => window.removeEventListener("globuy:auth-required", onAuthRequired);
  }, []);
  const navigate = useCallback((path: string, replace = false) => {
    if (replace) window.history.replaceState({}, "", path);
    else window.history.pushState({}, "", path);
    setPathname(path);
  }, []);
  const route = routeFromPath(pathname);
  if (user === undefined) return <main className="auth-loading"><span>{authError || "正在检查登录状态…"}</span>{authError && <button onClick={() => restoreAuth()}>重试</button>}</main>;
  if (user === null) {
    const target = pathname === "/" ? "/assistant" : pathname;
    return <AuthPage onAuthenticated={(authenticated) => { setUser(authenticated); navigate(target); }} />;
  }
  const logout = async () => {
    try { await authApi.logout(); } finally {
      localStorage.removeItem("globuy.active_thread_id");
      setUser(null);
      navigate("/");
    }
  };
  if (pathname === "/account") {
    return <AccountPage onBack={() => navigate("/assistant")} onLogout={() => void logout()} user={user} />;
  }
  if (pathname === "/wishlist") {
    return <WishlistPage onBack={() => navigate("/assistant")} onLogout={() => void logout()} user={user} />;
  }
  if (!route.assistant) {
    const previous = localStorage.getItem("globuy.active_thread_id");
    return <LandingPage onContinue={() => navigate(previous ? `/assistant/${encodeURIComponent(previous)}` : "/assistant")} onStart={() => navigate("/assistant")} />;
  }
  return <WorkbenchPage navigate={navigate} onLogout={() => void logout()} routeThreadId={route.threadId} user={user} />;
}
