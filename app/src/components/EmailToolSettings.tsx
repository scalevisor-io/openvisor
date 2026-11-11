import { useState } from "react";
import {
  toolsApi,
  type EmailSecurity,
  type EmailToolConfigPatch,
  type Tool,
} from "../lib/endpoints";
import { Spinner } from "./ui";

/** §Tools email row: the SMTP settings and the recipient allowlist, shown in
 * the tool's detail modal on /admin/tools. The password is write-only (the API
 * answers has_password), and the server refuses to enable the row - or to keep
 * it enabled through an SMTP change - until the saved settings log in. Builds
 * never see any of this: they get a per-project token, and the platform checks
 * every recipient against the project's customer plus the list below. */

const DEFAULT_PORT: Record<EmailSecurity, number> = { starttls: 587, ssl: 465, none: 25 };

function splitAddresses(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((a) => a.trim())
    .filter(Boolean);
}

export default function EmailToolSettings({
  tool,
  busy,
  onSave,
}: {
  tool: Tool;
  busy: boolean;
  onSave: (patch: EmailToolConfigPatch, doneMessage: string) => Promise<boolean>;
}) {
  const cfg = tool.email;
  const [host, setHost] = useState(cfg?.smtp_host ?? "");
  const [security, setSecurity] = useState<EmailSecurity>(cfg?.smtp_security ?? "starttls");
  const [port, setPort] = useState(String(cfg?.smtp_port ?? 587));
  const [username, setUsername] = useState(cfg?.smtp_username ?? "");
  const [password, setPassword] = useState("");
  const [fromAddress, setFromAddress] = useState(cfg?.from_address ?? "");
  const [fromName, setFromName] = useState(cfg?.from_name ?? "");
  const [recipients, setRecipients] = useState((cfg?.allowed_recipients ?? []).join("\n"));
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<{ ok: boolean; detail: string } | null>(null);
  if (!cfg) return null;

  function changeSecurity(next: EmailSecurity) {
    // Follow the mode's conventional port unless the admin typed their own.
    if (Number(port) === DEFAULT_PORT[security]) setPort(String(DEFAULT_PORT[next]));
    setSecurity(next);
  }

  async function saveSettings() {
    const ok = await onSave(
      {
        smtp_host: host.trim(),
        smtp_port: Number(port) || DEFAULT_PORT[security],
        smtp_security: security,
        smtp_username: username.trim(),
        from_address: fromAddress.trim(),
        from_name: fromName.trim(),
        allowed_recipients: splitAddresses(recipients),
        ...(password ? { smtp_password: password } : {}),
      },
      "Email settings saved.",
    );
    if (ok) setPassword("");
  }

  async function sendTest() {
    setTesting(true);
    try {
      setTest(await toolsApi.testEmail(tool.id));
    } catch (e) {
      setTest({ ok: false, detail: e instanceof Error ? e.message : "Test failed" });
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="field mt">
      <label>SMTP server</label>
      <p className="tiny faint" style={{ marginTop: 0 }}>
        The relay the agent's notifications leave through. Builds never receive these
        settings - the platform sends on their behalf.
      </p>
      <div className="grid-2">
        <div className="field">
          <label>Host</label>
          <input value={host} placeholder="smtp.example.com" onChange={(e) => setHost(e.target.value)} />
        </div>
        <div className="row gap-sm">
          <div className="field" style={{ flex: 1 }}>
            <label>Security</label>
            <select value={security} onChange={(e) => changeSecurity(e.target.value as EmailSecurity)}>
              <option value="starttls">STARTTLS</option>
              <option value="ssl">SSL/TLS</option>
              <option value="none">None (plain text)</option>
            </select>
          </div>
          <div className="field" style={{ width: "6.5rem" }}>
            <label>Port</label>
            <input
              type="number"
              min={1}
              max={65535}
              value={port}
              onChange={(e) => setPort(e.target.value)}
            />
          </div>
        </div>
      </div>
      <div className="grid-2">
        <div className="field">
          <label>Username</label>
          <input
            value={username}
            placeholder="leave empty for no login"
            autoComplete="off"
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>
        <div className="field">
          <label>Password</label>
          <input
            type="password"
            value={password}
            placeholder={cfg.has_password ? "•••••••• (saving replaces it)" : "SMTP password"}
            autoComplete="new-password"
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
      </div>
      <div className="grid-2">
        <div className="field">
          <label>From address</label>
          <input
            value={fromAddress}
            placeholder="notifications@example.com"
            onChange={(e) => setFromAddress(e.target.value)}
          />
        </div>
        <div className="field">
          <label>From name</label>
          <input
            value={fromName}
            placeholder="defaults to the platform name"
            onChange={(e) => setFromName(e.target.value)}
          />
        </div>
      </div>
      <div className="field">
        <label>Approved recipients</label>
        <p className="tiny faint" style={{ marginTop: 0 }}>
          A build may always write to its project's customer. List any other address it may
          write to, one per line - anything else is refused before it reaches SMTP.
        </p>
        <textarea
          rows={4}
          value={recipients}
          placeholder={"ops@example.com\nconsultant@example.com"}
          onChange={(e) => setRecipients(e.target.value)}
        />
      </div>
      <div className="row gap-sm mt wrap">
        <button className="btn btn-primary btn-sm" disabled={busy} onClick={saveSettings}>
          {busy ? <Spinner /> : "Save email settings"}
        </button>
        {cfg.has_password && (
          <button
            className="btn btn-sm btn-ghost"
            disabled={busy}
            onClick={() => onSave({ smtp_password: "" }, "SMTP password removed.")}
          >
            Remove password
          </button>
        )}
        <button
          className="btn btn-sm"
          disabled={busy || testing || !cfg.configured}
          title={cfg.configured ? "Sends one message to your own address" : "Save the SMTP settings first"}
          onClick={sendTest}
        >
          {testing ? <Spinner /> : "Send me a test email"}
        </button>
      </div>
      <p className="tiny faint mt">
        {cfg.configured
          ? "Tests use the saved settings. Enabling the tool, or changing the SMTP settings while it is on, re-checks the login first."
          : "Save a host and a From address to enable the tool."}
      </p>
      {test && (
        <p className={`tiny mt ${test.ok ? "ok-text" : "danger"}`}>
          {test.ok ? "✓ " : "✗ "}
          {test.detail}
        </p>
      )}
    </div>
  );
}
