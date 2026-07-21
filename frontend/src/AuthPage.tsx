import { Check, Eye, EyeSlash, GlobeHemisphereWest } from "@phosphor-icons/react";
import { FormEvent, useMemo, useRef, useState } from "react";
import { ApiClientError, authApi, type AuthUser } from "./api";
import heroIllustration from "./assets/globuy-hero.webp";
import brandMark from "./assets/globuy-mark.webp";

type Mode = "login" | "register";

function friendlyError(error: unknown) {
  if (!(error instanceof ApiClientError)) return "操作失败，请稍后重试";
  const messages: Record<string, string> = {
    INVALID_CREDENTIALS: "邮箱或密码不正确，请重新输入。",
    LOGIN_RATE_LIMITED: "登录尝试过于频繁，请稍后再试。",
    EMAIL_ALREADY_REGISTERED: "该邮箱已经注册，可以直接登录。",
    IDEMPOTENCY_KEY_REUSED: "这次注册请求已经失效，请重新提交。",
    CSRF_FAILED: "安全校验失败，请刷新页面后重试。",
    DATABASE_NOT_CONFIGURED: "服务尚未完成数据库配置。",
  };
  return messages[error.code] || error.message;
}

export function AuthPage({ onAuthenticated }: { onAuthenticated: (user: AuthUser) => void }) {
  const [mode, setMode] = useState<Mode>("login");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const registrationKey = useRef<string | null>(null);
  const rules = useMemo(() => ({
    length: password.length >= 8 && password.length <= 16,
    letter: /[A-Za-z]/.test(password),
    number: /\d/.test(password),
    spaces: password === password.trim(),
    match: Boolean(confirmPassword) && password === confirmPassword,
  }), [confirmPassword, password]);

  const switchMode = (next: Mode) => {
    setMode(next);
    setError(null);
    setPassword("");
    setConfirmPassword("");
    registrationKey.current = null;
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    if (mode === "register" && !Object.values(rules).every(Boolean)) {
      setError("请先完成全部密码规则。");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const email = String(form.get("email") || "").trim();
      let response;
      if (mode === "login") response = await authApi.login(email, password);
      else {
        registrationKey.current ||= crypto.randomUUID();
        response = await authApi.register(email, password, String(form.get("display_name") || "").trim(), registrationKey.current);
      }
      const verified = await authApi.me();
      onAuthenticated(verified.user || response.user);
    } catch (reason) {
      setError(friendlyError(reason));
      if (reason instanceof ApiClientError && reason.code === "IDEMPOTENCY_KEY_REUSED") registrationKey.current = null;
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="landing-page auth-landing">
      <section className="landing-art" aria-labelledby="auth-brand-title">
        <div className="landing-note"><GlobeHemisphereWest size={16} weight="duotone" /> Globe + Buy = Globuy</div>
        <div className="landing-illustration-wrap"><img alt="彩铅绘制的地球与装满商品的购物车" className="landing-illustration" src={heroIllustration} /></div>
        <div className="landing-wordmark"><span className="eyebrow">GLOBE-WIDE DISCOVERY</span><h1 id="auth-brand-title">Globuy</h1><p>把需求交给 Globuy。<br />登录后继续你的购物旅程。</p></div>
      </section>
      <section className="landing-entry auth-entry" aria-labelledby="auth-title">
        <div className="entry-topline"><img alt="" className="entry-mark" height="54" src={brandMark} width="54" /><span>Shopping intelligence</span></div>
        <div className="auth-copy">
          <span className="eyebrow">YOUR SHOPPING COMPANION</span>
          <h2 id="auth-title">{mode === "login" ? "欢迎回来。" : "创建你的账号。"}</h2>
        </div>
        <div className="auth-tabs" role="tablist" aria-label="认证方式">
          <button aria-selected={mode === "login"} onClick={() => switchMode("login")} role="tab">登录</button>
          <button aria-selected={mode === "register"} onClick={() => switchMode("register")} role="tab">注册</button>
        </div>
        <form className="landing-auth-form" onSubmit={submit}>
          {mode === "register" && <label><span>显示名称</span><input autoComplete="name" maxLength={100} name="display_name" onChange={() => { registrationKey.current = null; }} required /></label>}
          <label><span>邮箱</span><input autoComplete="email" name="email" onChange={() => { registrationKey.current = null; }} required type="email" /></label>
          <label><span>密码</span><span className="password-field"><input autoComplete={mode === "login" ? "current-password" : "new-password"} maxLength={mode === "register" ? 16 : 256} minLength={8} onChange={(event) => { setPassword(event.target.value); registrationKey.current = null; }} required type={showPassword ? "text" : "password"} value={password} /><button aria-label={showPassword ? "隐藏密码" : "显示密码"} onClick={() => setShowPassword((value) => !value)} type="button">{showPassword ? <EyeSlash /> : <Eye />}</button></span></label>
          {mode === "register" && <>
            <label><span>确认密码</span><input autoComplete="new-password" maxLength={16} onChange={(event) => setConfirmPassword(event.target.value)} required type={showPassword ? "text" : "password"} value={confirmPassword} /></label>
            <ul className="password-rules" aria-label="密码规则">
              <li className={rules.length ? "done" : ""}><Check />8～16 位</li><li className={rules.letter ? "done" : ""}><Check />至少一个字母</li><li className={rules.number ? "done" : ""}><Check />至少一个数字</li><li className={rules.spaces ? "done" : ""}><Check />首尾无空格</li><li className={rules.match ? "done" : ""}><Check />两次输入一致</li>
            </ul>
          </>}
          {error && <div className="auth-error" role="alert"><span>{error}</span>{error.includes("已经注册") && <button onClick={() => switchMode("login")} type="button">直接登录</button>}</div>}
          <button className="landing-primary auth-submit" disabled={busy} type="submit">{busy ? "请稍候…" : mode === "login" ? "登录并继续" : "注册并进入 Globuy"}</button>
        </form>
      </section>
    </main>
  );
}
