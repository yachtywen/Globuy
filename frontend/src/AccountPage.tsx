import { useState, type FormEvent } from "react";
import { ArrowLeft, Key, SignOut } from "@phosphor-icons/react";
import { Filing } from "./Filing";
import { authApi, type AuthUser } from "./api";

export function AccountPage({ user, onBack, onLogout }: {
  user: AuthUser;
  onBack: () => void;
  onLogout: () => void;
}) {
  const [email, setEmail] = useState(user.email);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setMessage(null);
    const normalized = email.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalized)) {
      setError("请输入有效的邮箱地址");
      return;
    }
    if (password.length < 8 || password.length > 256) {
      setError("新密码长度需为 8～256 位");
      return;
    }
    if (password !== confirm) {
      setError("两次输入的新密码不一致");
      return;
    }
    setPending(true);
    try {
      await authApi.changePassword(normalized, password);
      setMessage("改密成功，请使用新密码重新登录。");
      window.setTimeout(() => onLogout(), 1200);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "操作失败，请稍后重试");
      setPending(false);
    }
  };

  return (
    <main className="account-page">
      <header className="account-header">
        <button onClick={onBack}><ArrowLeft />返回工作台</button>
        <div><strong>{user.display_name}</strong><span>{user.email}</span></div>
        <button onClick={onLogout}><SignOut />退出登录</button>
      </header>
      <section className="account-section account-profile">
        <span className="section-label">ACCOUNT</span>
        <h1>账户信息</h1>
        <p className="account-section-intro">长期记忆由系统在后台自动整理，无需手动维护。</p>
      </section>
      <section className="account-section account-password">
        <span className="section-label">SECURITY</span>
        <h1>修改密码</h1>
        <p className="account-section-intro">直接输入账号邮箱与新密码即可修改（忘记原密码也可自助重置）；修改成功后所有登录会话失效，需用新密码重新登录。</p>
        <form className="password-change-form" onSubmit={submit}>
          <label><span>邮箱</span><input autoComplete="email" onChange={(event) => setEmail(event.target.value)} required type="email" value={email} /></label>
          <label><span>新密码</span><input autoComplete="new-password" maxLength={256} minLength={8} onChange={(event) => setPassword(event.target.value)} required type="password" value={password} /></label>
          <label><span>确认新密码</span><input autoComplete="new-password" maxLength={256} minLength={8} onChange={(event) => setConfirm(event.target.value)} required type="password" value={confirm} /></label>
          {error && <div className="auth-error" role="alert">{error}</div>}
          {message && <div className="password-ok" role="status">{message}</div>}
          <button disabled={pending} type="submit">{pending ? "提交中…" : "保存新密码"}</button>
        </form>
        <p className="password-tip"><Key size={14} weight="duotone" /> 建议使用与登录时不同的新密码，且不要与其它网站共用。</p>
      </section>
      <Filing />
    </main>
  );
}
