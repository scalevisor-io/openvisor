import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import AuthShell from "../components/AuthShell";
import Altcha from "../components/Altcha";
import { Alert, Spinner } from "../components/ui";
import { useAuth } from "../lib/auth";
import { authApi, metaApi } from "../lib/endpoints";
import { useAltcha } from "../lib/useAltcha";
import type { AccountType } from "../types";

export default function Signup() {
  const { settings } = useAuth();
  const brandName = settings?.brand_name ?? "Openvisor";
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [accountType, setAccountType] = useState<AccountType>("individual");
  const [companyName, setCompanyName] = useState("");
  const [fullName, setFullName] = useState("");
  const [acceptTerms, setAcceptTerms] = useState(false);
  const captcha = useAltcha();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [landingUrl, setLandingUrl] = useState("https://openvisor.example.com");

  useEffect(() => {
    metaApi
      .config()
      .then((c) => c.landing_base_url && setLandingUrl(c.landing_base_url))
      .catch(() => {});
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 10) {
      setError("Password must be at least 10 characters.");
      return;
    }
    if (!acceptTerms) {
      setError("Please accept the terms of service and privacy policy.");
      return;
    }
    if (!captcha.ready) {
      setError("Please complete the captcha.");
      return;
    }
    setBusy(true);
    try {
      await authApi.signup({
        email,
        password,
        account_type: accountType,
        company_name: accountType === "organization" ? companyName : undefined,
        full_name: accountType === "organization" ? fullName : undefined,
        altcha: captcha.payload,
        accept_terms: acceptTerms,
      });
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Signup failed.");
      // The solved challenge is burned; a retry needs a fresh one.
      captcha.reset();
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <AuthShell title="Check your inbox">
        <Alert kind="success">
          Account created. We sent a verification link to <strong>{email}</strong>. Verify your
          email before creating a project.
        </Alert>
        <Link to="/login" className="btn btn-block">
          Back to sign in
        </Link>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Create account" subtitle={`Start building an MVP with ${brandName}.`}>
      {error && <Alert kind="error">{error}</Alert>}
      {captcha.error && (
        <Alert kind="error">Could not load the captcha challenge. Reload to retry.</Alert>
      )}
      <form onSubmit={submit}>
        <div className="field">
          <label>Account type</label>
          <div className="row">
            <label className="checkbox-row">
              <input
                type="radio"
                name="acct"
                checked={accountType === "individual"}
                onChange={() => setAccountType("individual")}
              />
              Individual
            </label>
            <label className="checkbox-row">
              <input
                type="radio"
                name="acct"
                checked={accountType === "organization"}
                onChange={() => setAccountType("organization")}
              />
              Organization
            </label>
          </div>
        </div>

        {accountType === "organization" && (
          <>
            <div className="field">
              <label htmlFor="company">Company name</label>
              <input
                id="company"
                type="text"
                required
                value={companyName}
                onChange={(e) => setCompanyName(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="fullname">Your full name</label>
              <input
                id="fullname"
                type="text"
                required
                autoComplete="name"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
              />
              <div className="hint">The contact person for this company account.</div>
            </div>
          </>
        )}

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
            autoComplete="new-password"
            required
            minLength={10}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <div className="hint">Minimum 10 characters.</div>
        </div>

        <div className="field">
          <label className="checkbox-row">
            <input
              type="checkbox"
              required
              checked={acceptTerms}
              onChange={(e) => setAcceptTerms(e.target.checked)}
            />
            <span>
              I have read and accept the{" "}
              <a href={`${landingUrl}/terms`} target="_blank" rel="noreferrer">
                terms of service
              </a>{" "}
              and the{" "}
              <a href={`${landingUrl}/privacy`} target="_blank" rel="noreferrer">
                privacy policy
              </a>
              .
            </span>
          </label>
        </div>

        <Altcha challenge={captcha.challenge} onVerified={captcha.setPayload} />

        <button
          type="submit"
          className="btn btn-primary btn-block"
          disabled={busy || !captcha.ready}
        >
          {busy ? <Spinner /> : "Create account"}
        </button>
      </form>
      <div className="auth-links">
        <span className="muted">Already have an account?</span>
        <Link to="/login">Sign in</Link>
      </div>
    </AuthShell>
  );
}
