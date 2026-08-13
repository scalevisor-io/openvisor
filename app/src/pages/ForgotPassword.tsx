import { useState } from "react";
import { Link } from "react-router-dom";
import AuthShell from "../components/AuthShell";
import { Alert, Spinner } from "../components/ui";
import { authApi } from "../lib/endpoints";

export default function ForgotPassword() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await authApi.forgotPassword(email);
    } catch {
      // Endpoint always returns ok; never reveal account existence.
    } finally {
      setBusy(false);
      setSent(true);
    }
  }

  return (
    <AuthShell title="Reset password" subtitle="We'll email you a reset link.">
      {sent ? (
        <>
          <Alert kind="success">
            If an account exists for that email, a reset link is on its way.
          </Alert>
          <Link to="/login" className="btn btn-block">
            Back to sign in
          </Link>
        </>
      ) : (
        <form onSubmit={submit}>
          <div className="field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
            {busy ? <Spinner /> : "Send reset link"}
          </button>
          <div className="auth-links">
            <Link to="/login">Back to sign in</Link>
          </div>
        </form>
      )}
    </AuthShell>
  );
}
