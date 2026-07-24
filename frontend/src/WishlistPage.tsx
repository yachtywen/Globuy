import { ArrowDown, ArrowLeft, ArrowSquareOut, ArrowUp, CheckCircle, ClockCounterClockwise, DotsThree, MagnifyingGlass, ShoppingCart, SignOut, Trash, X } from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { wishlistApi, type AuthUser } from "./api";
import fallbackImage from "./assets/product-fallback.webp";
import brandMark from "./assets/globuy-mark.webp";
import type { PriceHistory, Wishlist, WishlistItem } from "./types";

function money(value: number | null, currency: string) {
  if (value === null) return null;
  return `${currency === "CNY" ? "¥" : `${currency} `}${value.toFixed(2)}`;
}

function dateTime(value: string | null) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

function PriceChange({ item }: { item: WishlistItem }) {
  if (item.current_price === null) return <span className="price-neutral">当前价格暂不可用</span>;
  if (item.price_change === null) return <span className="price-neutral">暂无可比较价格</span>;
  if (item.price_change < 0) return <span className="wishlist-price-down"><ArrowDown />下降 {money(Math.abs(item.price_change), item.currency)}{item.price_change_percent !== null && `（${Math.abs(item.price_change_percent).toFixed(1)}%）`}</span>;
  if (item.price_change > 0) return <span className="wishlist-price-up"><ArrowUp />上涨 {money(item.price_change, item.currency)}{item.price_change_percent !== null && `（${item.price_change_percent.toFixed(1)}%）`}</span>;
  return <span className="price-neutral">价格持平</span>;
}

function ObserverIllustration() {
  return <svg aria-hidden="true" className="wishlist-ip wishlist-ip-left" viewBox="0 0 240 300"><path d="M42 225c32-43 122-54 165-5" fill="none" stroke="#b8d7ce" strokeWidth="8" strokeLinecap="round" opacity=".45"/><circle cx="105" cy="128" r="67" fill="#b9dded" stroke="#638b82" strokeWidth="4"/><path d="M55 111c18-13 23-42 51-43 16 18 28 7 47 4 17 16 22 38 18 61-24-5-33 9-39 30-23-6-36-23-56-14-13-10-20-22-21-38Z" fill="#e8df79" opacity=".85"/><circle cx="83" cy="126" r="5" fill="#3e4945"/><circle cx="126" cy="126" r="5" fill="#3e4945"/><path d="M91 146c9 8 20 8 29 0" fill="none" stroke="#3e4945" strokeWidth="3" strokeLinecap="round"/><circle cx="163" cy="116" r="34" fill="none" stroke="#d7735d" strokeWidth="7"/><path d="m187 140 26 27" stroke="#d7735d" strokeWidth="9" strokeLinecap="round"/><path d="M82 62c-7-23 10-36 24-36s31 13 24 36" fill="none" stroke="#6e6258" strokeWidth="6"/><circle cx="79" cy="61" r="19" fill="#f8ecbf" stroke="#6e6258" strokeWidth="5"/><circle cx="133" cy="61" r="19" fill="#f8ecbf" stroke="#6e6258" strokeWidth="5"/></svg>;
}

function CartIllustration() {
  return <svg aria-hidden="true" className="wishlist-ip wishlist-ip-right" viewBox="0 0 260 260"><path d="M39 92h164l-18 93H63Z" fill="#f1c7b7" stroke="#aa6b59" strokeWidth="5"/><path d="M53 120h139M59 151h128M92 94l8 91m43-91-5 91" fill="none" stroke="#aa6b59" strokeWidth="3" opacity=".7"/><path d="M37 91 26 61H8" fill="none" stroke="#665f58" strokeWidth="7" strokeLinecap="round"/><circle cx="83" cy="207" r="13" fill="#6d7772"/><circle cx="169" cy="207" r="13" fill="#6d7772"/><path d="m85 54 12-24 18 19 21-25 11 30" fill="#efd868" stroke="#a8904d" strokeWidth="4" strokeLinejoin="round"/><path d="M157 52c13-19 41-5 37 16-3 18-37 37-37 37s-34-19-37-37c-4-21 24-35 37-16Z" fill="#da7661" opacity=".8"/></svg>;
}

function PriceChart({ history }: { history: PriceHistory }) {
  const points = useMemo(() => {
    const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
    const byDay = new Map<string, { price: number; time: string }>();
    [...history.items].sort((a, b) => Date.parse(a.observed_at) - Date.parse(b.observed_at)).forEach((item) => {
      const timestamp = Date.parse(item.observed_at);
      if (item.price !== null && Number.isFinite(timestamp) && timestamp >= cutoff) byDay.set(new Date(timestamp).toISOString().slice(0, 10), { price: item.price, time: item.observed_at });
    });
    return [...byDay.values()];
  }, [history]);
  if (!points.length) return <div className="chart-empty">近七日暂无有效价格数据。</div>;
  const values = points.map((point) => point.price);
  const min = Math.min(...values), max = Math.max(...values), range = Math.max(max - min, 1);
  const coords = points.map((point, index) => ({ ...point, x: points.length === 1 ? 180 : 28 + index * (304 / (points.length - 1)), y: 168 - ((point.price - min) / range) * 116 }));
  return <div className="price-chart"><svg aria-label="近七日价格趋势" role="img" viewBox="0 0 360 200"><g className="chart-grid"><line x1="28" x2="332" y1="52" y2="52"/><line x1="28" x2="332" y1="110" y2="110"/><line x1="28" x2="332" y1="168" y2="168"/></g><polyline points={coords.map((point) => `${point.x},${point.y}`).join(" ")} />{coords.map((point) => <circle cx={point.x} cy={point.y} key={point.time} r="4"><title>{money(point.price, history.currency)} · {dateTime(point.time)}</title></circle>)}</svg><div className="chart-summary"><span>最低 <strong>{money(min, history.currency)}</strong></span><span>最高 <strong>{money(max, history.currency)}</strong></span></div></div>;
}

function PriceHistoryModal({ item, onClose }: { item: WishlistItem; onClose: () => void }) {
  const [history, setHistory] = useState<PriceHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    document.body.classList.add("modal-open");
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    void wishlistApi.priceHistory(item.wishlist_item_id).then(setHistory).catch((reason) => setError(reason instanceof Error ? reason.message : "价格历史加载失败"));
    return () => { document.removeEventListener("keydown", onKey); document.body.classList.remove("modal-open"); previous?.focus(); };
  }, [item.wishlist_item_id, onClose]);
  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}><section aria-labelledby="price-history-title" aria-modal="true" className="price-history-modal" role="dialog"><header><div><span className="section-label">7-DAY PRICE TRACE</span><h2 id="price-history-title">价格详情</h2></div><button aria-label="关闭价格详情" className="icon-button" onClick={onClose} ref={closeRef}><X /></button></header><div className="history-product"><img alt="" onError={(event) => { event.currentTarget.src = fallbackImage; }} src={item.image_url || fallbackImage} /><div><strong>{item.title}</strong><span>当前 {money(item.current_price, item.currency) || "暂不可用"} · 加入时 {money(item.added_price, item.currency) || "暂不可用"}</span></div></div>{error ? <div className="wishlist-error">{error}</div> : history ? <PriceChart history={history} /> : <div className="chart-skeleton" aria-label="正在加载价格历史"><i/><i/><i/><i/></div>}<p className="history-updated">最近检查：{dateTime(item.last_checked_at) || "尚未完成首次价格检查"}</p></section></div>;
}

function WishlistCard({ item, busy, onDelete, onHistory, onRefresh, onStatus }: { item: WishlistItem; busy: boolean; onDelete: () => void; onHistory: () => void; onRefresh: () => void; onStatus: (status: "active" | "purchased") => void }) {
  const [menu, setMenu] = useState(false);
  const validUrl = item.product_url && /^https?:\/\//i.test(item.product_url);
  return <article className={`wishlist-card ${busy ? "is-busy" : ""}`}>
    <div className="wishlist-media"><img alt={item.title} loading="lazy" onError={(event) => { event.currentTarget.src = fallbackImage; }} src={item.image_url || fallbackImage} /><span>{item.platform}</span></div>
    <div className="wishlist-copy"><div className="wishlist-title-row"><h2>{item.title}</h2>{item.status === "purchased" && <span className="purchased-badge"><CheckCircle />已购买</span>}</div><div className="wishlist-facts"><span>加入时 <strong>{money(item.added_price, item.currency) || "价格暂不可用"}</strong></span><span>加入于 {dateTime(item.added_at) || "—"}</span>{item.target_price !== null && <span>目标价 {money(item.target_price, item.currency)}</span>}</div>{item.note && <p className="wishlist-note">{item.note}</p>}<div className="freshness"><span>{item.last_checked_at ? `最近更新：${dateTime(item.last_checked_at)}` : "尚未完成首次价格检查"}</span>{item.next_check_at && <span>预计下次检查：{dateTime(item.next_check_at)}</span>}</div>{item.last_error_code && <p className="stale-warning">最近一次价格检查失败，当前价格可能不是最新。</p>}</div>
    <div className="wishlist-actions"><div className="wishlist-current"><small>当前价格</small><strong>{money(item.current_price, item.currency) || "暂不可用"}</strong><PriceChange item={item} /></div><div className="wishlist-button-row">{validUrl ? <a href={item.product_url!} rel="noopener noreferrer" target="_blank">前往商城 <ArrowSquareOut /></a> : <button disabled title="暂未提供商品链接">前往商城</button>}<button onClick={onHistory}><MagnifyingGlass />价格详情</button><div className="more-wrap"><button aria-expanded={menu} aria-label="更多操作" onClick={() => setMenu((value) => !value)}><DotsThree /></button>{menu && <div className="more-menu"><button onClick={onRefresh}><ClockCounterClockwise />刷新价格</button>{item.status === "purchased" ? <button onClick={() => onStatus("active")}><ClockCounterClockwise />恢复关注</button> : <button onClick={() => onStatus("purchased")}><ShoppingCart />标记已购买</button>}<button className="danger" onClick={onDelete}><Trash />删除</button></div>}</div></div></div>
  </article>;
}

export function WishlistPage({ user, onBack, onLogout }: { user: AuthUser; onBack: () => void; onLogout: () => void }) {
  const [wishlist, setWishlist] = useState<Wishlist | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<WishlistItem | null>(null);
  const [deleting, setDeleting] = useState<WishlistItem | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const load = async (silent = false) => { if (!silent) setLoading(true); setError(null); try { setWishlist(await wishlistApi.getDefault()); } catch (reason) { setError(reason instanceof Error ? reason.message : "心愿库加载失败"); } finally { if (!silent) setLoading(false); } };
  useEffect(() => { void load(); }, []);
  const mutate = async (item: WishlistItem, action: () => Promise<unknown>) => { setBusyId(item.wishlist_item_id); setError(null); try { await action(); await load(true); } catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败，请重试"); } finally { setBusyId(null); } };
  const items = wishlist?.items || [];
  return <main className="wishlist-page"><ObserverIllustration/><CartIllustration/><header className="wishlist-header"><div className="brand"><img alt="" className="brand-mark" src={brandMark}/><div><strong>GLOBUY</strong><small>SHOPPING INTELLIGENCE</small></div></div><nav><button onClick={onBack}><ArrowLeft />返回聊天</button><button disabled={loading} onClick={() => void load()}><ClockCounterClockwise />刷新</button><span>{user.display_name}</span><button aria-label="退出登录" onClick={onLogout}><SignOut /></button></nav></header><div className="wishlist-main"><div className="wishlist-intro"><span className="section-label">CURATED & TRACKED</span><h1>我的心愿库</h1><p>收藏感兴趣的商品，持续查看价格变化。</p><div><strong>{items.length}</strong><span>件心愿商品</span>{wishlist && <small>{wishlist.name}</small>}</div></div>{error && <div className="wishlist-error" role="alert"><span>{error}</span><button onClick={() => void load()}>重试</button></div>}{loading ? <div className="wishlist-skeleton" aria-label="正在加载心愿库">{[1,2,3].map((key) => <i key={key}/>)}</div> : items.length ? <section className="wishlist-list" aria-label="心愿商品列表">{items.map((item) => <WishlistCard busy={busyId === item.wishlist_item_id} item={item} key={item.wishlist_item_id} onDelete={() => setDeleting(item)} onHistory={() => setSelected(item)} onRefresh={() => void mutate(item, () => wishlistApi.refreshPrice(item.wishlist_item_id))} onStatus={(status) => void mutate(item, () => wishlistApi.update(item.wishlist_item_id, { status }))}/>)}</section> : <section className="wishlist-empty"><CartIllustration/><h2>心愿库还是空的</h2><p>在 Agent 推荐商品中选择“加入心愿库”，我们会在这里持续记录价格变化。</p><button onClick={onBack}>返回聊天发现商品</button></section>}</div>{selected && <PriceHistoryModal item={selected} onClose={() => setSelected(null)}/>} {deleting && <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setDeleting(null)}><section aria-modal="true" className="confirm-dialog" role="dialog"><span className="section-label">REMOVE ITEM</span><h2>删除这件心愿商品？</h2><p>“{deleting.title}”将从心愿库移除。</p><div><button onClick={() => setDeleting(null)}>取消</button><button className="danger-button" onClick={() => { const item = deleting; setDeleting(null); void mutate(item, () => wishlistApi.remove(item.wishlist_item_id)); }}>确认删除</button></div></section></div>}</main>;
}
