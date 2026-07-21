import { ArrowRight, ClockCounterClockwise, GlobeHemisphereWest } from "@phosphor-icons/react";
import heroIllustration from "./assets/globuy-hero.webp";
import brandMark from "./assets/globuy-mark.webp";

export function LandingPage({ onContinue, onStart }: { onContinue: () => void; onStart: () => void }) {
  return (
    <main className="landing-page">
      <section className="landing-art" aria-labelledby="landing-brand-title">
        <div className="landing-note"><GlobeHemisphereWest size={16} weight="duotone" /> Globe + Buy = Globuy</div>
        <div className="landing-illustration-wrap">
          <img
            alt="彩铅绘制的地球与装满商品的购物车"
            className="landing-illustration"
            decoding="async"
            fetchPriority="high"
            height="1067"
            src={heroIllustration}
            width="1600"
          />
        </div>
        <div className="landing-wordmark">
          <span className="eyebrow">GLOBE-WIDE DISCOVERY</span>
          <h1 id="landing-brand-title">Globuy</h1>
          <p>把需求交给 Globuy。<br />从理解需求，到找到真正适合你的商品。</p>
        </div>
      </section>

      <section className="landing-entry" aria-labelledby="landing-entry-title">
        <div className="entry-topline">
          <img alt="" className="entry-mark" height="54" src={brandMark} width="54" />
          <span>Shopping intelligence</span>
        </div>
        <div className="entry-copy">
          <span className="eyebrow">YOUR SHOPPING COMPANION</span>
          <h2 className="hero-title" id="landing-entry-title"><span>找到更适合</span><span>你的商品。</span></h2>
          <p>描述需求，Globuy 将为你搜索、比较，并解释推荐原因。</p>
        </div>
        <div className="entry-actions">
          <button className="landing-primary" onClick={onStart}>开始选购 <ArrowRight size={18} weight="bold" /></button>
          <button className="landing-secondary" onClick={onContinue}><ClockCounterClockwise size={17} />继续上次会话</button>
        </div>
      </section>
    </main>
  );
}
