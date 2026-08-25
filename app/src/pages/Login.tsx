import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import AuthShell from "../components/AuthShell";
import Altcha from "../components/Altcha";
import { Alert, Spinner } from "../components/ui";
import { authApi } from "../lib/endpoints";
import { resetCsrf } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useAltcha } from "../lib/useAltcha";
import { ApiError } from "../lib/api";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { refresh } = useAuth();
  const captcha = useAltcha();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = params.get("next") || "/";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await authApi.login(email, password, captcha.payload);
      resetCsrf(); // session rotated → refetch CSRF on next mutation
      await refresh();
      navigate(next, { replace: true });
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? "Invalid email or password."
          : err instanceof Error
            ? err.message
            : "Login failed.",
      );
      // The solved challenge is burned; a retry needs a fresh one.
      captcha.reset();
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell title="Sign in" subtitle="Access your projects and demos.">
      {error && <Alert kind="error">{error}</Alert>}
      {captcha.error && (
        <Alert kind="error">Could not load the captcha challenge. Reload to retry.</Alert>
      )}
      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        <Altcha challenge={captcha.challenge} onVerified={captcha.setPayload} />

        <button
          type="submit"
          className="btn btn-primary btn-block"
          disabled={busy || !captcha.ready}
        >
          {busy ? <Spinner /> : "Sign in"}
        </button>
      </form>
      <div className="auth-links">
        <Link to="/forgot-password">Forgot password?</Link>
        <Link to="/signup">Create account</Link>
      </div>
    </AuthShell>
  );
}
