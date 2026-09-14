import { ArrowLeft, Check, Eye, EyeSlash, GlobeHemisphereWest } from "@phosphor-icons/react";
import { FormEvent, useMemo, useRef, useState } from "react";
import { ApiClientError, authApi, randomId, type AuthUser } from "./api";
import { Filing } from "./Filing";
import heroIllustration from "./assets/globuy-hero.webp";
import brandMark from "./assets/globuy-mark.webp";

type Mode = "login" | "register" | "reset";

function friendlyError(error: unknown) {
  if (!(error instanceof ApiClientError)) return "操作失败，请稍后重试";
  const messages: Record<string, string> = {
    INVALID_CREDENTIALS: "邮箱或密码不正确，请重新输入。",
    LOGIN_RATE_LIMITED: "登录尝试过于频繁，请稍后再试。",
    EMAIL_ALREADY_REGISTERED: "该邮箱已经注册，可以直接登录。",
    IDEMPOTENCY_KEY_REUSED: "这次注册请求已经失效，请重新提交。",
    CSRF_FAILED: "安全校验失败，请刷新页面后重试。",
    DATABASE_NOT_CONFIGURED: "服务尚未完成数据库配置。",
    ACCOUNT_NOT_FOUND: "该邮箱没有对应账号，请先注册。",
  };
  return messages[error.code] || error.message;
}

export function AuthPage({ onAuthenticated }: { onAuthenticated: (user: AuthUser) => void }) {
  const [mode, setMode] = useState<Mode>("login");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [email, setEmail] = useState("");
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
  const needsRules = mode === "register" || mode === "reset";

  const switchMode = (next: Mode) => {
    setMode(next);
    setError(null);
    setInfo(null);
    setPassword("");
    setConfirmPassword("");
    registrationKey.current = null;
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const submittedEmail = email.trim() || String(form.get("email") || "").trim();
    if (needsRules && !Object.values(rules).every(Boolean)) {
      setError("请先完成全部密码规则。");
      return;
    }
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      if (mode === "login") {
        const response = await authApi.login(submittedEmail, password);
        const verified = await authApi.me();
        onAuthenticated(verified.user || response.user);
        return;
      }
      if (mode === "register") {
        registrationKey.current ||= randomId();
        const response = await authApi.register(
          submittedEmail,
          password,
          String(form.get("display_name") || "").trim(),
          registrationKey.current,
        );
        const verified = await authApi.me();
        onAuthenticated(verified.user || response.user);
        return;
      }
      // reset：直接输入邮箱 + 新密码，无需旧密码（后端按用户要求如此设计）
      await authApi.changePassword(submittedEmail, password);
      setInfo("改密成功！请用新密码登录。");
      window.setTimeout(() => switchMode("login"), 1500);
    } catch (reason) {
      setError(friendlyError(reason));
      if (reason instanceof ApiClientError && reason.code === "IDEMPOTENCY_KEY_REUSED") registrationKey.current = null;
    } finally {
      setBusy(false);
    }
  };

  const showRules = needsRules;
  const showConfirm = needsRules;

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
          <h2 id="auth-title">{mode === "login" ? "欢迎回来。" : mode === "register" ? "创建你的账号。" : "重置密码"}</h2>
          {mode === "reset" && <p className="auth-copy-sub">输入账号邮箱与新密码即可直接修改（忘记原密码也可自助重置）。</p>}
        </div>
        {mode !== "reset" ? (
          <div className="auth-tabs" role="tablist" aria-label="认证方式">
            <button aria-selected={mode === "login"} onClick={() => switchMode("login")} role="tab">登录</button>
            <button aria-selected={mode === "register"} onClick={() => switchMode("register")} role="tab">注册</button>
          </div>
        ) : (
          <button className="auth-back-link" onClick={() => switchMode("login")} type="button"><ArrowLeft />返回登录</button>
        )}
        <form className="landing-auth-form" onSubmit={submit}>
          {mode === "register" && <label><span>显示名称</span><input autoComplete="name" maxLength={100} name="display_name" onChange={() => { registrationKey.current = null; }} required /></label>}
          <label><span>{mode === "reset" ? "账号邮箱" : "邮箱"}</span><input autoComplete="email" name="email" onChange={(event) => { setEmail(event.target.value); registrationKey.current = null; }} required type="email" value={email} /></label>
          <label><span>{needsRules ? "新密码" : "密码"}</span><span className="password-field"><input autoComplete={mode === "login" ? "current-password" : "new-password"} maxLength={needsRules ? 16 : 256} minLength={8} onChange={(event) => { setPassword(event.target.value); registrationKey.current = null; }} required type={showPassword ? "text" : "password"} value={password} /><button aria-label={showPassword ? "隐藏密码" : "显示密码"} onClick={() => setShowPassword((value) => !value)} type="button">{showPassword ? <EyeSlash /> : <Eye />}</button></span></label>
          {showConfirm && <>
            <label><span>确认密码</span><input autoComplete="new-password" maxLength={16} onChange={(event) => setConfirmPassword(event.target.value)} required type={showPassword ? "text" : "password"} value={confirmPassword} /></label>
            <ul className="password-rules" aria-label="密码规则">
              <li className={rules.length ? "done" : ""}><Check />8～16 位</li><li className={rules.letter ? "done" : ""}><Check />至少一个字母</li><li className={rules.number ? "done" : ""}><Check />至少一个数字</li><li className={rules.spaces ? "done" : ""}><Check />首尾无空格</li><li className={rules.match ? "done" : ""}><Check />两次输入一致</li>
            </ul>
          </>}
          {error && <div className="auth-error" role="alert"><span>{error}</span>{error.includes("已经注册") && <button onClick={() => switchMode("login")} type="button">直接登录</button>}{error.includes("不正确") && mode === "login" && <button onClick={() => switchMode("reset")} type="button">忘记密码？</button>}</div>}
          {info && <div className="password-ok" role="status">{info}</div>}
          <button className="landing-primary auth-submit" disabled={busy} type="submit">{busy ? "请稍候…" : mode === "login" ? "登录并继续" : mode === "register" ? "注册并进入 Globuy" : "重置密码"}</button>
          {mode === "login" && !error && <button className="auth-forgot" onClick={() => switchMode("reset")} type="button">忘记密码？</button>}
        </form>
        <Filing />
      </section>
    </main>
  );
}
