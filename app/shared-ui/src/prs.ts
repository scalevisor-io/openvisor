// PR/MR chip helpers (§PR chips), shared by both consoles' chat bubbles and
// request cards. The worker attaches structured refs as Message.meta.prs /
// Request.pr_urls: [{number, url, provider}].
import type { SharedMessage } from "./types";

export type PrRef = {
  number: number | string;
  url: string;
  provider?: "github" | "gitlab";
};

// The meta is platform-authored, but chips render as clickable links, so this
// validation is the same stored-XSS boundary MessageBody enforces: http/https
// URLs only, anything else is dropped (do not weaken).
export function validPrRefs(raw: unknown): PrRef[] {
  if (!Array.isArray(raw)) return [];
  const out: PrRef[] = [];
  for (const r of raw) {
    if (!r || typeof r !== "object") continue;
    const { url, number, provider } = r as { url?: unknown; number?: unknown; provider?: unknown };
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) continue;
    if (typeof number !== "number" && typeof number !== "string") continue;
    out.push({
      number,
      url,
      provider: provider === "github" || provider === "gitlab" ? provider : undefined,
    });
  }
  return out;
}

export function messagePrs(m: SharedMessage): PrRef[] {
  return validPrRefs((m.meta as { prs?: unknown } | null | undefined)?.prs);
}

// Some worker messages inline the raw URL in the body; when a chip renders the
// same URL, strip it from the text (the chip replaces it, like the spoke's
// request-deep-link button strips its URL).
export function stripPrUrls(text: string, refs: PrRef[]): string {
  let out = text;
  for (const r of refs) {
    out = out.split(`(${r.url})`).join("").split(r.url).join("");
  }
  return out
    .replace(/[ \t]+([.,;:)])/g, "$1")
    .replace(/:\s*$/gm, ".")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
