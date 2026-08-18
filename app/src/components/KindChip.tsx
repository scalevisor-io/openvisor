import type { ProjectKind } from "../types";
import { useAuth } from "../lib/auth";

const META: Record<ProjectKind, { label: string; cls: string }> = {
  ai: { label: "Curated AI", cls: "chip-ai" },
  direct_quote: { label: "Direct quote", cls: "chip-direct" },
  auto_dev: { label: "Auto-developer", cls: "chip-ai" },
  chat: { label: "Chat", cls: "chip-ai" },
  mcp: { label: "MCP", cls: "chip-ai" },
};

// Chip that labels a project as AI-built vs a human-quoted engagement, with a
// hover tooltip explaining the difference.
export default function KindChip({ kind }: { kind: ProjectKind }) {
  const { settings } = useAuth();
  const consultant = settings?.consultant_first_name ?? "Consultant";
  const tips: Record<ProjectKind, string> = {
    ai: `Senior-curated AI build: an agent builds a working MVP fast, plugged into ${consultant}'s experience knowledge base, with a live demo. Paid from prepaid credits.`,
    direct_quote: `Custom-made engagement quoted by ${consultant} - no automated build. For heavier or more demanding work (air-gapped systems, infrastructure refactoring/migration). No charge to submit.`,
    auto_dev: `Issue-driven sentinel: assign or label an issue on the watched repository and the ${consultant}-curated agent builds it and opens a pull request. Usage-based credits per build.`,
    chat: `A conversation with the ${consultant}-curated assistant, grounded in the experience knowledge base - no build pipeline. Opening fee plus usage-billed answers.`,
    mcp: `Worked through a coding agent over MCP: this project holds the model and the knowledge bases its token queries with, and every call bills to it. No build pipeline of its own.`,
  };
  const m = META[kind] ?? META.ai;
  const tip = tips[kind] ?? tips.ai;
  return (
    <span className={`chip ${m.cls} has-tip`} tabIndex={0} aria-label={tip}>
      {m.label}
      <span className="tip" role="tooltip">
        {tip}
      </span>
    </span>
  );
}
