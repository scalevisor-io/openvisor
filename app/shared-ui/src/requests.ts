// Shared request vocabulary used by both the spoke RequestsTab and the hub
// customer console, so type labels + status colours never drift between them.
import type { PrRef } from "./prs";

export type SharedRequest = {
  id: string;
  type: string;
  handling: string;
  status: string;
  title: string;
  created_at: string;
  price_credits?: number | null;
  tokens_consumed?: number | null;
  // §PR chips: PRs/MRs this request's dev runs opened, oldest first.
  pr_urls?: PrRef[] | null;
};

export const REQUEST_TYPE_LABELS: Record<string, string> = {
  mvp: "Initial build",
  feature: "New feature",
  edit: "Edit",
  bug: "Bug fix",
  production_deploy: "Production deploy",
};

// Map a request status to a semantic kind (ok|info|warn|err|muted) for badge hue.
export function requestStatusKind(status: string): string {
  const m: Record<string, string> = {
    proposed: "warn",
    open: "info",
    quoted: "warn",
    in_progress: "info",
    done: "ok",
    rejected: "err",
  };
  return m[status] ?? "muted";
}
