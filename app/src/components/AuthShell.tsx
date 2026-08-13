import type { ReactNode } from "react";
import { useAuth } from "../lib/auth";
import { BrandMark, BrandName } from "./ui";

export default function AuthShell({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  const { settings } = useAuth();
  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="auth-head">
          <div className="wordmark" style={{ justifyContent: "center", marginBottom: "0.5rem", gap: "0.45rem" }}>
            <BrandMark
              size={24}
              primary={settings?.brand_color_primary ?? "#22d3ee"}
              secondary={settings?.brand_color_secondary ?? "#7c3aed"}
            />
            <BrandName name={settings?.brand_name ?? "Openvisor"} />
          </div>
          <h1 style={{ marginTop: "1rem" }}>{title}</h1>
          {subtitle && <p className="muted small">{subtitle}</p>}
        </div>
        {children}
      </div>
    </div>
  );
}
