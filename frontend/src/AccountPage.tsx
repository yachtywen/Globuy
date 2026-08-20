import { FormEvent, useEffect, useRef, useState } from "react";
import { ArrowCounterClockwise, ArrowLeft, Check, SignOut, Sparkle, Trash, X } from "@phosphor-icons/react";
import { memoryApi, type AuthUser } from "./api";
import type { MemoryCandidate, MemoryEntry } from "./types";

const QUICK_MEMORIES = [
  { label: "性价比党", key: "value_for_money", content: "优先考虑高性价比、耐用且价格合理的商品。" },
  { label: "精致党", key: "refined_style", content: "偏好做工精致、细节考究、设计克制的商品。" },
  { label: "中国风", key: "chinese_style", content: "偏好具有中国文化元素、东方美学与现代融合的设计。" },
  { label: "传统风格", key: "traditional_style", content: "偏好经典、传统、耐看且不过度追逐潮流的设计。" },
  { label: "高端", key: "premium_style", content: "偏好高端定位、优质材质和成熟品牌体验。" },
  { label: "INS 风", key: "ins_style", content: "偏好简洁、明亮、适合社交媒体视觉表达的 INS 风格。" },
] as const;

const CATEGORY_NAMES: Record<MemoryEntry["category"], string> = {
  preference: "偏好",
  blacklist: "排除项",
  history: "历史信息",
};

export function AccountPage({ user, onBack, onLogout }: {
  user: AuthUser;
  onBack: () => void;
  onLogout: () => void;
}) {
  const formRef = useRef<HTMLFormElement>(null);
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([]);
  const [memoryView, setMemoryView] = useState<"active" | "archived">("active");
  const [selectedTag, setSelectedTag] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = async () => {
    try {
      const [memory, candidate] = await Promise.all([
        memoryApi.list(memoryView),
        memoryView === "active" ? memoryApi.candidates() : Promise.resolve({ items: [] }),
      ]);
      setMemories(memory.items);
      setCandidates(candidate.items);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "个人数据加载失败");
    }
  };

  useEffect(() => { void reload(); }, [memoryView]);

  const chooseQuickMemory = (choice: (typeof QUICK_MEMORIES)[number]) => {
    const form = formRef.current;
    if (!form) return;
    const category = form.elements.namedItem("category") as HTMLSelectElement | null;
    const key = form.elements.namedItem("key") as HTMLInputElement | null;
    const content = form.elements.namedItem("content") as HTMLTextAreaElement | null;
    if (category) category.value = "preference";
    if (key) key.value = choice.key;
    if (content) {
      content.value = choice.content;
      content.focus();
    }
    setSelectedTag(choice.key);
    setError(null);
  };

  const addMemory = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    setBusy(true);
    try {
      await memoryApi.create(
        String(data.get("category")) as MemoryEntry["category"],
        String(data.get("key")),
        String(data.get("content")),
      );
      form.reset();
      setSelectedTag(null);
      await reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "记忆保存失败");
    } finally {
      setBusy(false);
    }
  };

  const removeMemory = async (memory: MemoryEntry) => {
    try {
      await memoryApi.remove(memory.memory_id);
      await reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "记忆删除失败");
    }
  };

  const restoreMemory = async (memory: MemoryEntry) => {
    setBusy(true);
    try {
      await memoryApi.restore(memory.memory_id);
      await reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "记忆恢复失败");
    } finally {
      setBusy(false);
    }
  };

  const decideCandidate = async (candidate: MemoryCandidate, confirm: boolean) => {
    setBusy(true);
    try {
      if (confirm) await memoryApi.confirmCandidate(candidate.candidate_id);
      else await memoryApi.rejectCandidate(candidate.candidate_id);
      await reload();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "记忆候选处理失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="account-page">
      <header className="account-header">
        <button onClick={onBack}><ArrowLeft />返回工作台</button>
        <div><strong>{user.display_name}</strong><span>{user.email}</span></div>
        <button onClick={onLogout}><SignOut />退出登录</button>
      </header>
      {error && <p className="error-banner" role="alert">{error}</p>}
      <div className="account-grid">
        <section className="account-section memory-library">
          <span className="section-label">LONG-TERM MEMORY</span>
          <h1>你的长期记忆</h1>
          <p className="account-section-intro">Globuy 会在后续选购中持续参考这些偏好，让推荐越来越懂你。</p>
          <div className="memory-view-tabs" role="tablist" aria-label="长期记忆状态">
            <button aria-selected={memoryView === "active"} onClick={() => setMemoryView("active")} role="tab">当前记忆</button>
            <button aria-selected={memoryView === "archived"} onClick={() => setMemoryView("archived")} role="tab">已归档</button>
          </div>
          {memoryView === "active" && candidates.length > 0 && (
            <div className="memory-candidates" aria-label="待确认长期记忆">
              <strong>待你确认</strong>
              <p>这些内容由本次对话提炼，确认后才会用于后续推荐。</p>
              {candidates.map((candidate) => (
                <article key={candidate.candidate_id}>
                  <div>
                    <span>{CATEGORY_NAMES[candidate.category]}</span>
                    <strong>{candidate.key}</strong>
                    <p>{candidate.content}</p>
                  </div>
                  <div className="memory-candidate-actions">
                    <button aria-label={`确认 ${candidate.key}`} disabled={busy} onClick={() => void decideCandidate(candidate, true)}><Check /></button>
                    <button aria-label={`拒绝 ${candidate.key}`} disabled={busy} onClick={() => void decideCandidate(candidate, false)}><X /></button>
                  </div>
                </article>
              ))}
            </div>
          )}
          {memories.length === 0 ? (
            <div className="memory-empty">
              <Sparkle size={22} />
              <strong>还没有长期记忆</strong>
              <p>从右侧选择一个风格标签，或写下你的预算、材质与品牌偏好。</p>
            </div>
          ) : (
            <div className="memory-list">
              {memories.map((item) => (
                <article key={item.memory_id}>
                  <div>
                    <span>{CATEGORY_NAMES[item.category]}</span>
                    <strong>{item.key}</strong>
                    <p>{item.content}</p>
                  </div>
                  {memoryView === "archived" ? (
                    <button aria-label={`恢复 ${item.key}`} disabled={busy} onClick={() => void restoreMemory(item)}><ArrowCounterClockwise /></button>
                  ) : (
                    <button aria-label={`删除 ${item.key}`} disabled={busy} onClick={() => void removeMemory(item)}><Trash /></button>
                  )}
                </article>
              ))}
            </div>
          )}
        </section>

        <section className="account-section memory-composer">
          <span className="section-label">ADD MEMORY</span>
          <h1>添加一条记忆</h1>
          <p className="account-section-intro">先选一个常用标签快速填写，也可以直接写下只属于你的偏好。</p>
          <div className="quick-memory-block">
            <strong>快速选择</strong>
            <div className="quick-memory-tags" aria-label="常用偏好标签">
              {QUICK_MEMORIES.map((choice) => (
                <button
                  aria-pressed={selectedTag === choice.key}
                  className={selectedTag === choice.key ? "selected" : ""}
                  key={choice.key}
                  onClick={() => chooseQuickMemory(choice)}
                  type="button"
                >
                  {choice.label}
                </button>
              ))}
            </div>
          </div>
          <form className="memory-form" onSubmit={addMemory} ref={formRef}>
            <label>记忆类型<select defaultValue="preference" name="category"><option value="preference">偏好</option><option value="blacklist">排除项</option><option value="history">历史信息</option></select></label>
            <label>偏好名称<input maxLength={128} name="key" placeholder="例如：budget_style" required /></label>
            <label>具体内容<textarea maxLength={4000} name="content" placeholder="例如：更重视性价比，耳机预算通常在 500 元以内" required /></label>
            <button disabled={busy} type="submit">{busy ? "正在保存…" : "添加记忆"}</button>
          </form>
        </section>
      </div>
    </main>
  );
}
