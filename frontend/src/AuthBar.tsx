import { useState } from "react";
import { Lock, LogOut, ShieldCheck } from "lucide-react";
import { fetchJson } from "./api";
import type { AuthStatus } from "./types";

interface AuthBarProps {
  auth: AuthStatus | null;
  onAuthChange: (next: AuthStatus) => void;
}

export function AuthBar({ auth, onAuthChange }: AuthBarProps) {
  const [username, setUsername] = useState("operator");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!auth) return null;

  if (!auth.auth_required) {
    return (
      <span className="auth-pill" title="Operator write endpoints open in local_open mode">
        <ShieldCheck size={13} /> Local Operator Mode
      </span>
    );
  }

  async function login(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const next = await fetchJson<AuthStatus>("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      setPassword("");
      onAuthChange(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  async function logout() {
    setBusy(true);
    try {
      const next = await fetchJson<AuthStatus>("/api/auth/logout", { method: "POST" });
      onAuthChange(next);
    } finally {
      setBusy(false);
    }
  }

  if (auth.authenticated) {
    return (
      <div className="auth-pill auth-ok">
        <ShieldCheck size={13} />
        <span>Signed in: {auth.username ?? "operator"}</span>
        <button type="button" className="link-btn" onClick={logout} disabled={busy}>
          <LogOut size={12} /> Sign out
        </button>
      </div>
    );
  }

  return (
    <form className="auth-form" onSubmit={login} aria-label="Operator sign in">
      <Lock size={13} />
      <input
        aria-label="Operator username"
        value={username}
        onChange={(e) => setUsername(e.target.value)}
        placeholder="operator"
      />
      <input
        type="password"
        aria-label="Operator password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="Password"
      />
      <button type="submit" className="refresh-button" disabled={busy || !password}>
        {busy ? "Signing in..." : "Sign in"}
      </button>
      {error ? <small className="critical-text">{error}</small> : null}
    </form>
  );
}
