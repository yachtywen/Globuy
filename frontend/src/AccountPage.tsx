import { FormEvent, useEffect, useState } from "react";
import { ArrowLeft, SignOut, Trash } from "@phosphor-icons/react";
import { memoryApi, memorySkillApi, type AuthUser } from "./api";
import type { MemoryEntry, MemorySkill } from "./types";

const categoryNames: Record<MemoryEntry["category"], string> = { preference: "偏好", blacklist: "排除项", history: "历史信息" };

export function AccountPage({ user, onBack, onLogout }: { user: AuthUser; onBack: () => void; onLogout: () => void }) {
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [skills, setSkills] = useState<MemorySkill[]>([]);
  const [activeSkillId, setActiveSkillId] = useState<string | null>(null);
  const [editing, setEditing] = useState<MemoryEntry | null>(null);
  const [creatingDomain, setCreatingDomain] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const reload = async () => {
    try {
      const [memoryResponse, skillResponse] = await Promise.all([memoryApi.list(), memorySkillApi.list()]);
      setMemories(memoryResponse.items); setSkills(skillResponse.items);
      setActiveSkillId((current) => current && skillResponse.items.some((skill) => skill.skill_id === current) ? current : skillResponse.items[0]?.skill_id || null);
      setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "加载失败"); }
  };
  useEffect(() => { void reload(); }, []);
  const activeSkill = skills.find((skill) => skill.skill_id === activeSkillId) || null;
  const activeMemories = memories.filter((memory) => memory.skill_id === activeSkillId);
  const submitMemory = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try {
      if (!activeSkillId) throw new Error("请先选择一个偏好领域");
      await memoryApi.create(String(data.get("category")) as MemoryEntry["category"], String(data.get("key")), String(data.get("content")), activeSkillId);
      event.currentTarget.reset(); await reload();
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "记忆保存失败"); }
  };
  const submitSkill = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    try { await memorySkillApi.create(String(data.get("name")), String(data.get("description")), String(data.get("keywords")).split(/[，,]/).map((value) => value.trim()).filter(Boolean)); event.currentTarget.reset(); await reload(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "偏好领域创建失败"); }
  };
  return <main className="account-page">
    <header className="account-header"><button onClick={onBack}><ArrowLeft />返回工作台</button><div><strong>{user.display_name}</strong><span>{user.email}</span></div><button onClick={onLogout}><SignOut />退出登录</button></header>
    {error && <p className="error-banner" role="alert">{error}</p>}
    <div className="account-grid">
      <section className="account-section memory-library"><span className="section-label">PREFERENCE SETTINGS</span><h1>偏好设置</h1><p className="account-section-intro">按购物领域保存偏好，Globuy 会在对话时自动选择相关领域的信息。</p>
        <button className="new-domain-button" onClick={() => setCreatingDomain(true)} type="button">新建偏好领域</button>
        <div className="skill-list">{skills.map((skill) => <button className={skill.skill_id === activeSkillId ? "selected" : ""} key={skill.skill_id} onClick={() => setActiveSkillId(skill.skill_id)} type="button"><span>{skill.name}</span><small>{skill.memory_count} 条</small></button>)}</div>
        {activeSkill && <div className="skill-detail"><strong>{activeSkill.name}</strong><p>{activeSkill.description}</p><button className="text-button" onClick={() => void memorySkillApi.update(activeSkill.skill_id, { is_enabled: !activeSkill.is_enabled }).then(reload)} type="button">{activeSkill.is_enabled ? "暂停此领域" : "启用此领域"}</button>{activeSkill.name !== "通用偏好" && <button className="text-button danger-text" onClick={() => { if (confirm(`删除「${activeSkill.name}」后，其中记忆会转入通用偏好。`)) void memorySkillApi.remove(activeSkill.skill_id).then(reload); }} type="button">删除领域</button>}</div>}
        <div className="memory-list">{activeMemories.length ? activeMemories.map((memory) => <article key={memory.memory_id}><div><span>{categoryNames[memory.category]}</span><strong>{memory.key}</strong><p>{memory.content}</p><small>{memory.source === "agent_confirmed" ? "来自对话确认" : "手动添加"}</small></div><button onClick={() => setEditing(memory)} type="button">编辑</button><button aria-label={`删除 ${memory.key}`} onClick={() => void memoryApi.remove(memory.memory_id).then(reload)}><Trash /></button></article>) : <p className="panel-empty">这个偏好领域还没有内容。</p>}</div>
      </section>
      <section className="account-section memory-composer"><span className="section-label">ADD PREFERENCE</span><h1>添加偏好</h1><p className="account-section-intro">将购物习惯写入对应领域，下一次对话时会自动参考。</p>
        <form className="memory-form" onSubmit={submitMemory}><label>偏好领域<select name="skill_id" value={activeSkillId || ""} onChange={(event) => setActiveSkillId(event.target.value)} required>{skills.map((skill) => <option key={skill.skill_id} value={skill.skill_id}>{skill.name}</option>)}</select></label><label>偏好类型<select defaultValue="preference" name="category"><option value="preference">偏好</option><option value="blacklist">排除项</option><option value="history">历史信息</option></select></label><label>名称<input maxLength={128} name="key" required /></label><label>具体内容<textarea maxLength={4000} name="content" required /></label><button type="submit" disabled={!activeSkillId}>保存偏好</button></form>
      </section>
    </div>
    {creatingDomain && <div className="comparison-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setCreatingDomain(false)}><form className="memory-editor" onSubmit={async (event) => { await submitSkill(event); setCreatingDomain(false); }}><h2>新建偏好领域</h2><label>领域名称<input maxLength={80} name="name" required /></label><label>领域说明<textarea maxLength={500} name="description" required /></label><label>触发关键词（用逗号分隔）<input name="keywords" required /></label><div><button onClick={() => setCreatingDomain(false)} type="button">取消</button><button type="submit">创建领域</button></div></form></div>}
    {editing && <div className="comparison-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setEditing(null)}><form className="memory-editor" onSubmit={async (event) => { event.preventDefault(); const data = new FormData(event.currentTarget); await memoryApi.update(editing.memory_id, { key: String(data.get("key")), content: String(data.get("content")), category: String(data.get("category")) as MemoryEntry["category"], skill_id: String(data.get("skill_id")) }); setEditing(null); await reload(); }}><h2>编辑偏好</h2><label>名称<input defaultValue={editing.key} name="key" required /></label><label>内容<textarea defaultValue={editing.content} name="content" required /></label><label>类型<select defaultValue={editing.category} name="category"><option value="preference">偏好</option><option value="blacklist">排除项</option><option value="history">历史信息</option></select></label><label>偏好领域<select defaultValue={editing.skill_id || ""} name="skill_id">{skills.map((skill) => <option key={skill.skill_id} value={skill.skill_id}>{skill.name}</option>)}</select></label><div><button onClick={() => setEditing(null)} type="button">取消</button><button type="submit">保存修改</button></div></form></div>}
  </main>;
}
