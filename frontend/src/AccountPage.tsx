import { ArrowLeft, SignOut } from "@phosphor-icons/react";
import type { AuthUser } from "./api";

export function AccountPage({ user, onBack, onLogout }: {
  user: AuthUser;
  onBack: () => void;
  onLogout: () => void;
}) {
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
    </main>
  );
}
