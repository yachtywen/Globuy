import { ArrowSquareOut, Check, Heart, Scales, X } from "@phosphor-icons/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import fallbackImage from "./assets/product-fallback.webp";
import { memoryApi, memorySkillApi, wishlistApi } from "./api";
import { visibleResultStrings } from "./presentation";
import type { Artifact, MemoryCandidate, MemorySkill, TaskResult } from "./types";

type Product = {
  id: string;
  offerId: string | null;
  wishlistEligible: boolean;
  title: string;
  imageUrl: string | null;
  productUrl: string | null;
  platform: string;
  price: number | null;
  currency: string;
  rating: number | null;
  sales: number | null;
  reasons: string[];
  warnings: string[];
  specifications: Record<string, string>;
};

function stringValue(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) if (typeof record[key] === "string" && record[key]) return String(record[key]);
  return null;
}

function numberValue(record: Record<string, unknown>, key: string) {
  return typeof record[key] === "number" && Number.isFinite(record[key]) ? Number(record[key]) : null;
}

function stringList(value: unknown) {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && Boolean(item)) : [];
}

function normalizeProduct(raw: Record<string, unknown>, index: number): Product {
  const attributes = raw.attributes && typeof raw.attributes === "object" && !Array.isArray(raw.attributes)
    ? Object.fromEntries(Object.entries(raw.attributes as Record<string, unknown>).slice(0, 8).map(([key, value]) => [key, String(value)]))
    : {};
  return {
    id: stringValue(raw, ["item_id", "id"]) || `candidate-${index}`,
    offerId: stringValue(raw, ["offer_id"]),
    wishlistEligible: raw.wishlist_eligible !== false,
    title: stringValue(raw, ["title", "name", "product_name"]) || `候选商品 ${index + 1}`,
    imageUrl: stringValue(raw, ["image_url", "imageUrl"]),
    productUrl: stringValue(raw, ["product_url", "url", "link"]),
    platform: stringValue(raw, ["platform", "source"]) || "来源待核验",
    price: numberValue(raw, "price"),
    currency: stringValue(raw, ["currency"]) || "CNY",
    rating: numberValue(raw, "rating"),
    sales: numberValue(raw, "sales"),
    reasons: visibleResultStrings(stringList(raw.reasons)),
    warnings: visibleResultStrings(stringList(raw.flags)),
    specifications: attributes,
  };
}

function platformLabel(value: string) {
  return ({ taobao: "淘宝", jingdong: "京东", douyin: "抖音" } as Record<string, string>)[value] || value;
}

function memoryKeyLabel(key: string) {
  if (key === "budget_max_cny") return "预算上限";
  if (key.startsWith("explicit_preference_")) return "本次偏好";
  return key;
}

function suggestedSkillId(content: string, skills: MemorySkill[]) {
  const rules: Array<[string, string[]]> = [
    ["数码设备", ["耳机", "手机", "电脑", "平板", "键盘", "鼠标", "相机", "显示器", "数码"]],
    ["服饰穿搭", ["衣服", "穿", "鞋", "裤", "裙", "尺码", "身高", "体重"]],
    ["家居生活", ["家居", "家具", "床", "枕", "厨房", "清洁", "收纳"]],
    ["美妆护肤", ["护肤", "美妆", "口红", "肤质", "肤色", "敏感"]],
    ["运动户外", ["运动", "跑步", "健身", "露营", "户外"]],
  ];
  const target = rules.find(([, words]) => words.some((word) => content.includes(word)))?.[0] || "通用偏好";
  return skills.find((skill) => skill.name === target)?.skill_id || skills.find((skill) => skill.name === "通用偏好")?.skill_id || skills[0]?.skill_id || null;
}

function ProductImage({ product }: { product: Product }) {
  const [failed, setFailed] = useState(false);
  const rawSource = failed || !product.imageUrl ? fallbackImage : product.imageUrl;
  const source = rawSource === fallbackImage ? rawSource : `/api/v1/product-image?image_url=${encodeURIComponent(rawSource)}`;
  return <img alt={failed || !product.imageUrl ? "商品图片暂缺，显示 Globuy 手绘占位图" : product.title} height="180" loading="lazy" onError={() => setFailed(true)} referrerPolicy="no-referrer" src={source} width="220" />;
}

function Comparison({ products, onClose }: { products: Product[]; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); previous?.focus(); };
  }, [onClose]);
  const rows = [
    ["价格", (product: Product) => product.price === null ? "待核验" : `${product.currency === "CNY" ? "¥" : product.currency} ${product.price.toFixed(2)}`],
    ["平台", (product: Product) => platformLabel(product.platform)],
    ["评分", (product: Product) => product.rating === null ? null : product.rating.toFixed(1)],
    ["销量", (product: Product) => product.sales === null ? null : product.sales.toLocaleString("zh-CN")],
    ["入选依据", (product: Product) => product.reasons.join("；") || "按检索顺位入选"],
    ["注意事项", (product: Product) => product.warnings.join("；") || "无额外提示"],
  ] satisfies Array<[string, (product: Product) => string | null]>;
  const visibleRows = rows.filter(([, read]) => products.some((product) => read(product) !== null));
  return (
    <div className="comparison-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section aria-labelledby="comparison-title" aria-modal="true" className="comparison-panel" role="dialog">
        <div className="comparison-heading"><div><span className="section-label">SIDE BY SIDE</span><h2 id="comparison-title">商品对比</h2></div><button aria-label="关闭商品对比" className="icon-button" onClick={onClose} ref={closeRef}><X size={19} /></button></div>
        <div className="comparison-scroll"><table><caption className="sr-only">所选商品的已知字段对比</caption><thead><tr><th>对比项</th>{products.map((product) => <th key={product.id}>{product.title}</th>)}</tr></thead><tbody>{visibleRows.map(([label, read]) => <tr key={label}><th>{label}</th>{products.map((product) => <td key={product.id}>{read(product) ?? ""}</td>)}</tr>)}</tbody></table></div>
      </section>
    </div>
  );
}

function ArtifactList({ artifacts }: { artifacts: Artifact[] }) {
  if (!artifacts.length) return null;
  return <div className="artifact-list"><h3>可下载产物</h3>{artifacts.map((artifact) => <a href={artifact.download_url} key={artifact.file_id}><span><strong>{artifact.filename}</strong><small>{artifact.kind} · {Math.max(1, Math.round(artifact.size / 1024))} KB</small></span><ArrowSquareOut size={16} /></a>)}</div>;
}

export function ProductResults({ artifacts, result, sourceThreadId = null, sourceRunId = null }: {
  artifacts: Artifact[];
  result: TaskResult | null;
  sourceThreadId?: string | null;
  sourceRunId?: string | null;
}) {
  const products = useMemo(() => (result?.picks || []).map((pick, index) => normalizeProduct(pick, index)), [result]);
  const platformOutcomes = result?.platform_outcomes || [];
  const unresolved = useMemo(() => visibleResultStrings(result?.unresolved), [result]);
  const [selected, setSelected] = useState<string[]>([]);
  const [comparisonOpen, setComparisonOpen] = useState(false);
  const [saved, setSaved] = useState<string[]>([]);
  const [saving, setSaving] = useState<string | null>(null);
  const [wishlistError, setWishlistError] = useState<string | null>(null);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [skills, setSkills] = useState<MemorySkill[]>([]);
  const [candidates, setCandidates] = useState<MemoryCandidate[]>([]);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const autoOpenedMemoryKey = useRef<string | null>(null);
  const openMemoryCandidates = useCallback(async () => {
    const items = (result?.learned_preferences || []).map((item, index) => {
      const value = typeof item === "object" && item ? item as Record<string, unknown> : {};
      const rawKey = String(value.key || `preference_${index + 1}`);
      return { key: memoryKeyLabel(rawKey), category: (value.category === "blacklist" || value.category === "history" ? value.category : "preference") as MemoryCandidate["category"], content: String(value.content || item), confidence: typeof value.confidence === "number" ? value.confidence : 1 };
    });
    try { const response = await memorySkillApi.list(); setSkills(response.items); setCandidates(items.map((item) => ({ ...item, skill_id: suggestedSkillId(item.content, response.items) }))); setMemoryOpen(true); }
    catch (reason) { setMemoryError(reason instanceof Error ? reason.message : "无法加载 Skill"); }
  }, [result]);
  useEffect(() => {
    if (result?.status !== "complete" || !result.learned_preferences?.length) return;
    const key = `${sourceRunId || "no-run"}:${result.learned_preferences.map((item) => {
      const value = typeof item === "object" && item ? item as Record<string, unknown> : {};
      return String(value.key || value.content || item);
    }).join("|")}`;
    if (autoOpenedMemoryKey.current === key) return;
    autoOpenedMemoryKey.current = key;
    void openMemoryCandidates();
  }, [openMemoryCandidates, result, sourceRunId]);
  if (!result && artifacts.length === 0) return null;
  const toggle = (id: string) => setSelected((items) => items.includes(id) ? items.filter((item) => item !== id) : items.length < 4 ? [...items, id] : items);
  const compared = products.filter((product) => selected.includes(product.id));
  return (
    <section className="result-panel" aria-labelledby="result-heading">
      <div className="result-heading-row"><div><span className="section-label">SHORTLIST</span><h2 id="result-heading">为你筛出的商品</h2></div></div>
      {platformOutcomes.length > 0 && <div aria-label="平台检索状态" className="platform-outcomes">{platformOutcomes.map((outcome) => <span className={`platform-outcome ${outcome.status}`} key={outcome.platform}>{platformLabel(outcome.platform)} {outcome.status === "ok" ? `${outcome.candidate_count} 件候选` : outcome.status === "not_configured" ? "未配置" : "检索失败"}</span>)}</div>}
      {products.length ? <div className="product-list">{products.map((product, index) => {
        const checked = selected.includes(product.id);
        return <article className="product-card" key={product.id}>
          <div className="product-media"><span className="product-rank">#{String(index + 1).padStart(2, "0")}</span><ProductImage product={product} /></div>
          <div className="product-copy"><div className="product-meta"><span>{platformLabel(product.platform)}</span>{product.rating !== null && <span>评分 {product.rating.toFixed(1)}</span>}{product.sales !== null && <span>销量 {product.sales.toLocaleString("zh-CN")}</span>}</div><h3>{product.title}</h3><ul>{(product.reasons.length ? product.reasons : ["按检索顺位与已知约束筛选"]).map((reason) => <li key={reason}><Check size={14} weight="bold" />{reason}</li>)}</ul>{product.warnings.length > 0 && <p className="product-warning">{product.warnings.join(" · ")}</p>}</div>
          <div className="product-actions"><div><small>快照价格</small><strong>{product.price === null ? "待核验" : `${product.currency === "CNY" ? "¥" : product.currency} ${product.price.toFixed(2)}`}</strong></div><button aria-pressed={checked} className={`compare-toggle ${checked ? "selected" : ""}`} onClick={() => toggle(product.id)}>{checked ? <Check size={15} weight="bold" /> : <Scales size={15} />} {checked ? "已加入对比" : "加入对比"}</button><button className={`wishlist-toggle ${saved.includes(product.id) ? "selected" : ""}`} disabled={!product.offerId || !product.wishlistEligible || saving === product.id || saved.includes(product.id)} onClick={async () => { if (!product.offerId || !product.wishlistEligible) return; const clientRequestId = `wishlist_${crypto.randomUUID()}`; setSaving(product.id); setWishlistError(null); try { await wishlistApi.add(product.offerId, sourceThreadId, sourceRunId, clientRequestId); setSaved((items) => [...items, product.id]); } catch (reason) { setWishlistError(reason instanceof Error ? reason.message : "加入心愿库失败"); } finally { setSaving(null); } }} title={!product.offerId || !product.wishlistEligible ? "商品报价尚未保存，暂时无法加入心愿库。" : undefined}><Heart size={15} weight={saved.includes(product.id) ? "fill" : "regular"} />{saved.includes(product.id) ? "已加入心愿库" : saving === product.id ? "正在加入…" : "加入心愿库"}</button>{product.productUrl && /^https?:\/\//i.test(product.productUrl) ? <a href={product.productUrl} rel="noopener noreferrer" target="_blank">前往来源 <ArrowSquareOut size={15} /></a> : <span className="source-unavailable">来源链接未提供</span>}</div>
        </article>;
      })}</div> : result && <div className="result-empty"><strong>暂未形成结构化商品清单</strong><span>完整建议仍保留在上方回答中。</span></div>}
      {selected.length > 0 && <div aria-live="polite" className="compare-bar"><span>已选 {selected.length} / 4 件</span><button disabled={selected.length < 2} onClick={() => setComparisonOpen(true)}><Scales size={17} />对比商品</button></div>}
      {wishlistError && <div className="result-note" role="alert"><strong>心愿库操作失败</strong><p>{wishlistError}</p></div>}
      {unresolved.length ? <div className="result-note"><strong>仍需确认</strong><p>{unresolved.join(" · ")}</p></div> : null}
      {result?.learned_preferences?.length ? <div className="preference-note"><strong>发现 {result.learned_preferences.length} 条可长期保留的偏好</strong><p>查看、修改后再写入长期记忆。</p><button onClick={() => void openMemoryCandidates()} type="button">查看并保存</button>{memoryError && <small>{memoryError}</small>}</div> : null}
      <ArtifactList artifacts={artifacts} />
      {comparisonOpen && <Comparison onClose={() => setComparisonOpen(false)} products={compared} />}
      {memoryOpen && <div className="comparison-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setMemoryOpen(false)}><section aria-labelledby="memory-candidate-title" aria-modal="true" className="memory-editor memory-candidate-dialog" role="dialog"><h2 id="memory-candidate-title">确认长期记忆</h2><p>可修改或移除不需要的内容，确认后才会保存。</p>{candidates.map((candidate, index) => <div className="candidate-row" key={`${candidate.key}-${index}`}><input aria-label="记忆名称" value={candidate.key} onChange={(event) => setCandidates((items) => items.map((item, current) => current === index ? { ...item, key: event.target.value } : item))}/><textarea aria-label="记忆内容" value={candidate.content} onChange={(event) => setCandidates((items) => items.map((item, current) => current === index ? { ...item, content: event.target.value } : item))}/><select value={candidate.category} onChange={(event) => setCandidates((items) => items.map((item, current) => current === index ? { ...item, category: event.target.value as MemoryCandidate["category"] } : item))}><option value="preference">偏好</option><option value="blacklist">排除项</option><option value="history">历史信息</option></select><select value={candidate.skill_id || ""} onChange={(event) => setCandidates((items) => items.map((item, current) => current === index ? { ...item, skill_id: event.target.value } : item))}>{skills.map((skill) => <option key={skill.skill_id} value={skill.skill_id}>{skill.name}</option>)}</select><button onClick={() => setCandidates((items) => items.filter((_, current) => current !== index))} type="button">不保存</button></div>)}<div className="candidate-actions"><button onClick={() => setMemoryOpen(false)} type="button">取消</button><button disabled={!candidates.length || !sourceThreadId || !sourceRunId} onClick={async () => { try { const response = await memoryApi.confirm(candidates, sourceThreadId || "", sourceRunId || ""); if (response.conflicts.length) setMemoryError(`以下名称已存在：${response.conflicts.map((item) => item.key).join("、")}`); else setMemoryOpen(false); } catch (reason) { setMemoryError(reason instanceof Error ? reason.message : "保存失败"); } }} type="button">确认保存</button></div></section></div>}
    </section>
  );
}
