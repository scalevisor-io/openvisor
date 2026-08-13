import { useState } from "react";
import { authApi } from "../lib/endpoints";
import { useAuth } from "../lib/auth";
import { Spinner } from "./ui";

export default function ResendVerify() {
  const { me } = useAuth();
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [email, setEmail] = useState(me?.user.email ?? "");

  async function resend() {
    setBusy(true);
    try {
      await authApi.resendVerification(email);
      setSent(true);
    } catch {
      setSent(true); // endpoint always reports ok
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return <p className="muted small">Verification email sent to {email}. Check your inbox.</p>;
  }

  return (
    <div className="row" style={{ maxWidth: 420 }}>
      <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
      <button className="btn" onClick={resend} disabled={busy || !email}>
        {busy ? <Spinner /> : "Resend verification"}
      </button>
    </div>
  );
}
